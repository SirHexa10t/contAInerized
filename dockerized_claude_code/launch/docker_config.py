"""Docker-side launcher orchestration — everything between "we picked an agent"
and `docker run`. The image-build chain (ensure_image), the bind-mount
accumulator that flattens into `-v` flags (set_container_mounts +
add_docker_mount + mount_target_is_staged), small `docker` CLI wrappers
(require_docker, detect_docker_gid, docker_check_running_subprocess,
wait_for_container_running, docker_exec_root_subprocess,
docker_running_instances_subprocess, docker_check_any_agent_running_subprocess,
docker_subprocess), the
image-naming helper (image_tag), the tag.docker flag emitters
(build_arg_flags / env_forward_flags / entrypoint_chain), the post-build
install-failure surfacing (prompt_install_failures), and the container
invocation itself (run_container).

Plain `docker build` / `docker run` throughout — the compose layer is retired.
Static per-tag container config (build-arg names, cap_add, entrypoint, mounts,
env forwards) is declared in each tag's `tag.docker` and arrives here as the
Instance's DockerContribution records; dynamic values (resolved firewall
addresses, detected DOCKER_GID) are staged into container_env by the
tag handlers and pulled by name at flag-emission time.

Sister accumulator lives in container_env: `_container_env` for env-var
staging. This module holds:
  - _docker_mounts: {source: "target[:ro]"} — staged via add_docker_mount,
    flattened inline by run_container into `-v` flags.

Imports from paths (filesystem constants), claude_code_config (terminal title),
container_env (env staging + flag formatters), and network (the {firewall}
coordination hooks). tag_handlers imports add_docker_mount +
docker_check_any_agent_running_subprocess + detect_docker_gid from here;
run.py is the top-level consumer.
"""

import fcntl
import fnmatch
import os
import pty
import re
import select
import shutil
import struct
import subprocess
import sys
import termios
import time
import tty
from collections.abc import Callable, Iterable
from pathlib import Path

from .claude_code_config import set_terminal_title
from .container_env import (
    ContainerEnvKey, conf_env_args, container_env_args, stage_container_env,
    staged_env,
)
from .file_access import ensure_dir
from .firewall import (
    is_critical_pending, selftest_address, start_firewall_updater,
    wait_for_critical_addresses,
)
from .paths import (
    BASE_DOCKERFILE, CLAUDE_CONFIG_IN_CONTAINER, COWORK_IN_CONTAINER,
    DEFAULT_WORKSPACE, DOCKER_BASE_MOUNTS, DOCKERIZED_CLAUDE_ROOT,
    FIREWALL_DONE_IN_CONTAINER, INSTALL_FAILURES_LOG_IN_CONTAINER,
    LOCAL_BIN_IN_CONTAINER, RO_MOUNT_OPTION, cowork_dir_path,
    state_commands_dir, state_settings_path,
)
from .tags import DockerContribution, Instance
from .template_code.docker_prompts import (
    BUILDING_STEP, FIREWALL_WAITING, INSTALL_FAILURES_BODY, INSTALL_FAILURES_HEADER,
)
from .utils import call_or_exit, exit_if_missing, prompt_keypress, shell_capture, shell_returncode


# ============================================================
# Docker volume accumulator
# ============================================================
# Every bind-mount for `docker run` flows through this dict. set_container_mounts
# stages the always-on set (paths.DOCKER_BASE_MOUNTS + the per-instance
# workspace/state dirs, plus the {cowork} group-hosting dir when that tag is
# active); tag_handlers stages each active tag's declarative
# `tag.docker` mounts plus the [code] cache mounts; user_additions stages
# skills + optional creds. Mirror of container_env's `_container_env` /
# stage_container_env pattern — declarations flow one way, emission stays in
# this module.

_docker_mounts: dict[str, str] = {}   # {source_path_str: "target_path[:ro]"} — source uniquely identifies a mount across our callers


def add_docker_mount(source: Path | str, target: Path | str) -> None:
    """Stage a bind-mount for the upcoming `docker run` invocation. Any
    docker access-mode suffix (`:ro`, also `:z`/`:Z`, `:cached`/`:delegated`,
    propagation modes) is the caller's responsibility — bake it into target
    when needed. Both args coerce to str at this boundary so callers can pass
    Path objects without thinking about it.

    Re-staging an identical (source, target) pair is an idempotent no-op.
    A *conflicting* duplicate raises RuntimeError: the same target from a
    different source would emit two `-v` flags docker rejects at run time
    (or the accumulator's source-keying would silently drop one mount for
    same-source/new-target) — better a clean launcher error at staging time
    than a cryptic docker one later. User-reachable clashes (`home/`
    contents-mounts) are pre-checked with a friendlier message in
    user_additions before ever reaching this guard."""
    src, tgt = str(source), str(target)
    staged = _docker_mounts.get(src)
    if staged is not None and staged != tgt:
        raise RuntimeError(f"bind-mount source {src} is already staged at {staged}; refusing to re-stage it at {tgt}")
    bare_target = tgt.split(":", 1)[0]
    if any(v.split(":", 1)[0] == bare_target and s != src for s, v in _docker_mounts.items()):
        raise RuntimeError(f"bind-mount target {bare_target} is already staged from a different source; refusing to shadow it with {src}")
    _docker_mounts[src] = tgt


def mount_target_is_staged(target: Path | str) -> bool:
    """True if any prior `add_docker_mount` call has already staged a mount at
    the given target (the access-mode suffix on the staged value, if any, is
    ignored). Used by overlay callers like `user_additions.home_overlay_mounts`
    to refuse to shadow a launcher-owned mount."""
    target_str = str(target)
    return any(v.split(":", 1)[0] == target_str for v in _docker_mounts.values())


# ============================================================
# Image naming
# ============================================================

def image_tag(layer_names: list[str]) -> str:
    """The docker image tag for a build-layer sequence. [] → 'claude-agents:base'.
    ['code', 'dood'] → 'claude-agents:code.dood'. Layer names are already
    lowercase (tag names are folder names)."""
    if not layer_names:
        return "claude-agents:base"
    return "claude-agents:" + ".".join(layer_names)


# ============================================================
# Docker subprocess helpers
# ============================================================
# Every docker-CLI touchpoint outside orchestration lives here: the PATH
# presence check (require_docker), the read-only probes used by firewall
# coordination + cache pruning (detect_docker_gid,
# docker_check_running_subprocess, wait_for_container_running,
# docker_exec_root_subprocess, docker_running_instances_subprocess,
# docker_check_any_agent_running_subprocess),
# and the `docker` invocation wrapper (docker_subprocess) used by
# ensure_image / run_container below in the orchestration section.
# CONTAINER_NAME_PREFIX is the one place the per-launch container name format
# is defined — run_container builds container names from it, and
# docker_running_instances_subprocess both filters `docker ps` by it and strips
# it back off to recover instance ids; keeping them consistent is a one-line
# change here.

CONTAINER_NAME_PREFIX = "claude-code_"   # prefix for every per-launch container name (run_container) and the filter/strip used to map containers back to instance ids (docker_running_instances_subprocess)


# ============================================================
# Dry-run flag
# ============================================================
# Module-level toggle gating docker_subprocess's actual subprocess invocation.
# Set once at startup from run.py:launch via set_dry_run(); the default False
# means "real run" so callers that import this module without going through
# launch() (tests, audit) behave normally. The flag lives here rather than
# threaded through every function because the only operation it affects is
# the docker call itself — every other orchestration step (mount staging,
# env staging, firewall coordination, banner printing) happens identically
# in both modes, which is what makes --dry-run a faithful projection of a
# real run.

_dry_run = False


def set_dry_run(value: bool) -> None:
    """Set the module-level dry-run flag. Called from run.py:launch after CLI
    parsing. docker_subprocess checks this to gate its underlying
    subprocess.call — every surrounding step still runs so the user sees an
    accurate projection of what a real run would do."""
    global _dry_run
    _dry_run = value


def require_docker() -> None:
    """Exit early with a clean, verbose message when docker isn't usable — either
    not on PATH, or installed but the daemon isn't responding. On the daemon
    case the message carries the client version (and flags the server as
    unreachable) so a docker problem surfaces with concrete version numbers here
    at startup, instead of a deeper docker traceback mid-launch. Called on every
    launch (dry-run included — a faithful projection needs a live daemon just as
    a real run does)."""
    exit_if_missing(
        shutil.which("docker"),
        "docker is required but was not found in PATH. Install Docker "
        "(Desktop on macOS, Engine >= 20.10 on Linux; see README) and retry.",
    )
    probe = shell_capture("docker", "version")
    if probe.returncode != 0:
        detail = (probe.stderr or probe.stdout).strip().splitlines()
        sys.exit(
            "docker is installed but not responding — the daemon looks down.\n"
            f"  client version: {_docker_client_version() or 'unknown'}\n"
            "  server version: unreachable (is Docker Desktop / the daemon running?)\n"
            f"  docker said: {detail[-1] if detail else 'docker version exited non-zero'}\n"
            "Start Docker and retry."
        )


def _docker_client_version() -> str | None:
    """The docker CLIENT version string (readable without a running daemon), or
    None if it can't be determined."""
    r = shell_capture("docker", "version", "--format", "{{.Client.Version}}")
    return r.stdout.strip() if r.returncode == 0 and r.stdout.strip() else None


def detect_docker_gid() -> str | None:
    """Return the host's docker group GID as a string, or None if no docker
    group exists (or `getent` is unavailable — e.g. non-Linux hosts). Used by
    tag_handlers._apply_dood to stage DOCKER_GID for the `_dood` layer's
    Dockerfile, so claude can read/write the bind-mounted /var/run/docker.sock."""
    try:
        result = shell_capture("getent", "group", "docker")
    except FileNotFoundError:
        return None  # no getent (e.g., not Linux)
    if result.returncode == 0 and (out := result.stdout.strip()):
        return out.split(":")[2]
    return None


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


def docker_subprocess(args: list[str]) -> None:
    """Run `docker <args>`; sys.exit with the return code on non-zero, return
    silently on success. On dry-run (set via set_dry_run), print what would
    have been invoked and return without touching subprocess — every
    surrounding orchestration step still runs, so dry-run projects accurately.

    Both real-run callers want the exit-on-failure shape: ensure_image needs
    to continue to the next chain step on success but die if any build fails;
    run_container is the program's terminal — on success the unwind through
    launch() → __main__ exits the process with 0 naturally, equivalent to an
    explicit sys.exit(0)."""
    if _dry_run:
        print(f"  (dry-run: would invoke `docker {' '.join(args)}`)")
        return
    if (ret := shell_returncode("docker", *args)) != 0:
        sys.exit(ret)


def docker_stream_subprocess(args: list[str], on_lines: Callable[[Iterable[str]], None]) -> None:
    """Run `docker <args>` with stdout piped line-by-line into `on_lines`
    (quickie's stream-json renderer). stderr inherits the terminal; stdin is
    /dev/null so a stream-mode `claude` doesn't stall waiting for input. Exits
    with the container's return code on failure, mirroring docker_subprocess;
    dry-run prints the would-be invocation and skips it (the renderer never
    runs, matching docker_subprocess's no-op)."""
    if _dry_run:
        print(f"  (dry-run: would invoke `docker {' '.join(args)}`)")
        return
    with subprocess.Popen(["docker", *args], stdin=subprocess.DEVNULL,
                          stdout=subprocess.PIPE, text=True) as proc:
        assert proc.stdout is not None
        on_lines(proc.stdout)
    if proc.returncode:
        sys.exit(proc.returncode)


def docker_running_instances_subprocess() -> frozenset[str] | None:
    """The instance ids (`<agent>__<session>`) whose containers are running
    right now — one `docker ps` for all of them — or None when the state
    couldn't be determined (`docker ps` failed / daemon unreachable).

    None is a distinct third state, not an empty set, because the two callers
    want OPPOSITE failure behaviour: prune_caches must assume "something might
    be running" and skip deleting, while the picker must assume "mark nothing"
    rather than flag every row. Returning the unknown separately lets one
    docker call serve both.

    `--filter name=` is a substring/regex match against docker's own names
    (stored with a leading `/`), so it only NARROWS the listing cheaply — the
    authoritative check is `startswith(CONTAINER_NAME_PREFIX)` here, keeping
    CONTAINER_NAME_PREFIX the single source of truth for the name format and
    making the parse unit-testable without a daemon."""
    try:
        r = shell_capture("docker", "ps", "--filter", f"name={CONTAINER_NAME_PREFIX}", "--format", "{{.Names}}")
    except OSError:
        return None      # docker not on PATH at all — same "can't tell" as a failed probe
    if r.returncode != 0:
        return None
    return frozenset(name.removeprefix(CONTAINER_NAME_PREFIX)
                     for line in r.stdout.splitlines()
                     if (name := line.strip()).startswith(CONTAINER_NAME_PREFIX))


def docker_check_any_agent_running_subprocess() -> bool:
    """True if any agent container is currently running, OR if the running
    state couldn't be determined (conservative — treat the unknown as 'might
    be running' so the caller skips its cleanup). Used by
    tag_handlers.prune_caches as the 'is it safe to delete cache files' guard."""
    running = docker_running_instances_subprocess()
    return running is None or bool(running)


def running_instance_report(inst: Instance) -> str | None:
    """A one-line refusal message when `inst` already has a live container,
    else None — same shape as agents_crud.invalid_tags_report so run.py's two
    launch guards read alike.

    One check covers every route to a launch: a CLI target, a picker row whose
    running-snapshot went stale while the menu sat open, and a brand-new
    session name that happens to collide with a live container. It runs on the
    fully-resolved identity before anything is persisted or built, because
    `docker run --name` would refuse the duplicate anyway — this turns that
    late, raw daemon error into an early, readable one. Re-probes rather than
    reusing the picker's snapshot: freshness at launch time is the whole point."""
    running = docker_running_instances_subprocess()
    if running is None or inst.instance not in running:
        return None
    return (f"  Instance '{inst.instance}' is already running "
            f"(container {CONTAINER_NAME_PREFIX}{inst.instance}).\n"
            f"  Stop that container, or switch to the terminal that's running it.")


# ============================================================
# Injection — typing a prompt into a live session's TTY
# ============================================================
# The one thing group hosting needs docker for beyond launching: waking a
# running instance. Every byte of {cowork} DATA moves as ordinary files through
# the per-participant mount; this is only the doorbell.
#
# It has to be a pty rather than a pipe. The launcher starts instances with `-t`,
# and the docker CLI refuses to attach non-TTY stdin to a TTY container — it
# exits at once, and the first write then dies with EPIPE. Two further details
# were each found the hard way and are load-bearing, so they are commented where
# they happen: raw mode (below) and window size (`_match_container_winsize`).

ENTER_KEY = "\r"                    # what the TUI reads as Enter; "\n" is swallowed by the input widget
INJECT_ENTER_DELAY = 0.4            # settle time between the text and Enter, so the TUI registers a full line
INJECT_ATTACH_PROBE = 1.0           # how long to watch for docker refusing the attach before typing
FALLBACK_TTY_SIZE = (24, 80)        # conventional terminal, used only when the container's size is unreadable
_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[a-zA-Z]|\x1b[()][A-Z0-9]|\x1b[=>]|\r")


def docker_attach_inject(instance: str, prompt: str,
                         *, enter_delay: float = INJECT_ENTER_DELAY) -> bool:
    """Type `prompt` into `instance`'s live session and press Enter. True if it
    landed.

    Deliberately no liveness pre-check: callers polling several participants
    already hold a `docker_running_instances_subprocess` snapshot, and a second
    probe per injection would buy nothing — attaching to a container that is gone
    fails cleanly and reports docker's own complaint.

    `--sig-proxy=false` keeps a Ctrl-C in the hub's terminal out of the agent's
    session. The attach is terminated rather than having its stdin closed: with a
    TTY the other attachers hold the master open, so `claude` never sees an EOF
    and the human's own session is left untouched.

    A single line only — `prompt` is TYPED, so an embedded newline reads as Enter
    and submits a fragment. Callers send a pointer to a file for anything longer
    (see cowork.mailbox.pointer_prompt)."""
    container = f"{CONTAINER_NAME_PREFIX}{instance}"
    master, slave = pty.openpty()
    _match_container_winsize(master, container)
    # Raw mode so the line discipline leaves our bytes alone: without it ICRNL
    # rewrites the Enter (\r -> \n) and ECHO bounces the prompt back into the
    # stream we read. Real docker sets raw itself; doing it here makes the
    # injection behave the same whether or not it gets that far.
    tty.setraw(master)
    proc = subprocess.Popen(["docker", "attach", "--sig-proxy=false", container],
                            stdin=slave, stdout=slave, stderr=slave, close_fds=True)
    os.close(slave)                          # the child owns it now
    try:
        # A rejected attach dies within moments and complains onto the pty, so
        # surface that rather than letting the first write fail with a bare EPIPE.
        early = _drain_pty(master, INJECT_ATTACH_PROBE)
        if proc.poll() is not None:
            detail = _ANSI_RE.sub("", early).strip() or "(docker printed nothing)"
            print(f"  Injection into '{instance}' failed: {detail}")
            return False
        os.write(master, prompt.encode())
        time.sleep(enter_delay)              # let the TUI register the line before Enter
        os.write(master, ENTER_KEY.encode())
        return True
    except OSError as e:
        print(f"  Injection into '{instance}' failed writing to the attach stream: {e}")
        return False
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
        os.close(master)


def container_tty_size(container: str) -> tuple[int, int] | None:
    """(rows, cols) of `container`'s OWN terminal, or None if unknowable.

    pid 1 in the container is `claude` (the image's ENTRYPOINT), so its fd 0 is
    the pty we are about to attach to, and `stty size` reports that pty's
    winsize. Run without `-t` so the exec doesn't allocate a pty of its own."""
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


def _match_container_winsize(fd: int, container: str) -> None:
    """Stamp the container's current window size onto our pty.

    `docker attach` PROPAGATES the client terminal's size to the container, so
    without this the pty's default (often 0x0) resizes the agent's terminal and
    its TUI redraws into nothing until the human resizes and triggers SIGWINCH.
    Matching the container's size makes the propagated resize a no-op.

    Read fresh on every injection, because the human may have resized or moved
    their window since the last prompt — a stale size is exactly what blanks the
    display."""
    size = container_tty_size(container) or FALLBACK_TTY_SIZE
    fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", size[0], size[1], 0, 0))


def _drain_pty(master: int, seconds: float) -> str:
    """Read whatever the attach stream emits for `seconds`, as text. Used to
    catch docker's own refusal before anything is typed."""
    chunks: list[bytes] = []
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        ready, _, _ = select.select([master], [], [], 0.2)
        if not ready:
            continue
        try:
            data = os.read(master, 65536)
        except OSError:                      # slave side closed — nothing more coming
            break
        if not data:
            break
        chunks.append(data)
    return b"".join(chunks).decode(errors="replace")


# ============================================================
# tag.docker flag emitters
# ============================================================

def build_arg_flags(forward: tuple[str, ...]) -> list[str]:
    """`--build-arg NAME=VALUE` flags for a layer's `[build] arg_forward`
    list, values pulled from the staged container env. Glob patterns expand
    against the staged keys (`INSTALL_*` → every staged INSTALL_<TOOL>);
    a plain name that isn't staged is silently skipped — the Dockerfile's
    own ARG default then applies (that's how DOCKER_GID keeps its 999
    fallback when detection is bypassed)."""
    staged = staged_env()
    out: list[str] = []
    for pattern in forward:
        if any(ch in pattern for ch in "*?["):
            keys = sorted(fnmatch.filter(staged, pattern))
        else:
            keys = [pattern] if pattern in staged else []
        out += [arg for k in keys for arg in ("--build-arg", f"{k}={staged[k]}")]
    return out


def env_forward_flags(contributions: list[DockerContribution]) -> list[str]:
    """`-e NAME=VALUE` flags for every `[run] env_forward` name across the
    active tags' contributions, values pulled from the staged container env.
    Unstaged names are silently skipped — that's the gating: {firewall}'s
    WHITELIST_ADDRESSES is only staged when the resolve actually ran."""
    staged = staged_env()
    names = [n for c in contributions for n in c.env_forward]
    return [arg for n in names if n in staged for arg in ("-e", f"{n}={staged[n]}")]


def entrypoint_chain(contributions: list[DockerContribution]) -> tuple[list[str], list[str]]:
    """`(flags, inner)` — the `--entrypoint` flag plus the links that follow it.

    Several tags can legitimately want to wrap the agent: `{firewall}` applies
    iptables first, `{muxer}` starts it inside a multiplexer. They compose as a
    CHAIN rather than competing: the first script becomes docker's entrypoint and
    every later one is handed to it as arguments, so each wrapper does its own job
    and then `exec "$@"`s the next. `{firewall}`'s script ends that way for
    exactly this reason (it used to `exec claude` and could not be followed).

    Order comes from `contributions`, which is already in chain order
    (`identity._ordered_groups`), and it matters: the firewall must be applied
    before anything else runs, and its `sudo -k` must happen before the agent
    starts. `test_docker_config` pins firewall-before-muxer so the ordering is a
    checked property rather than a coincidence of directory names.

    A bare script name resolves to LOCAL_BIN_IN_CONTAINER (where the owning tag's
    mounts put it); a path is used as-is. No overrides at all → `([], [])`, and the
    image's own ENTRYPOINT (claude) applies."""
    entrypoints = [c.entrypoint for c in contributions if c.entrypoint]
    if not entrypoints:
        return [], []
    resolved = [ep if "/" in ep else f"{LOCAL_BIN_IN_CONTAINER}/{ep}"
                for ep in entrypoints]
    return ["--entrypoint", resolved[0]], resolved[1:]


# ============================================================
# Orchestration
# ============================================================

def set_container_mounts(inst_id: Instance) -> None:
    """Stage per-launch bind-mounts via add_docker_mount. Sister to set_container_env
    (bind-mounts vs env vars); both run sequentially in setup_state. Two layers:
    the per-instance pair (workspace → /workspace, state dir → /home/claude/.claude)
    derived from inst_id, plus the always-on DOCKER_BASE_MOUNTS from paths.py
    (whose target strings already carry any `:ro` suffix). A `{cowork}` instance
    additionally gets its group-hosting dir at COWORK_IN_CONTAINER — see below.

    The launcher-generated settings file (base settings + policy fragments,
    written by agents_crud.install_settings) mounts READ-ONLY over
    `~/.claude/settings.json` — the file mount shadows the state-dir's rw
    view of the same path, so the agent can't relax its own policies.

    Workspace fallback: if `inst_id.workspace` is None (stale store entry that
    survived all the upstream prompts, or a session constructed without a
    workspace), default to DEFAULT_WORKSPACE so the bind-mount still resolves
    to a real host directory rather than crashing the docker invocation.

    Workspace access mode: read-only when a `workspace_readonly` specialty
    (e.g. `{frozen}`) is active — the mount itself denies writes, a harder
    guarantee than the `read-only` policy's harness-level tool denial. The
    state dir stays read-write regardless (Claude Code writes history/memory
    there)."""
    workspace_target = "/workspace" + (f":{RO_MOUNT_OPTION}" if inst_id.workspace_readonly else "")
    add_docker_mount(inst_id.workspace or DEFAULT_WORKSPACE, workspace_target)
    add_docker_mount(inst_id.state_dir, CLAUDE_CONFIG_IN_CONTAINER)
    if inst_id.is_cowork:
        # {cowork}: this instance's group-hosting dir, read-write. Created here
        # rather than by the hub because docker fixes mounts at container
        # creation — a missing source would be created by docker as a
        # root-owned dir the container's `claude` user could not write.
        # The `_cowork` Stop hook writes captures under this mount, which is
        # what makes them outlive the container.
        cowork_dir = cowork_dir_path(inst_id.instance)
        ensure_dir(cowork_dir)
        add_docker_mount(cowork_dir, str(COWORK_IN_CONTAINER))
    add_docker_mount(state_settings_path(inst_id.state_dir),
                     f"{CLAUDE_CONFIG_IN_CONTAINER}/settings.json:{RO_MOUNT_OPTION}")
    # The commands dir is ASSEMBLED per instance (shared commands + each active
    # command the active tags declare) by agents_crud.install_commands, then mounted whole and
    # read-only. Mounting the shared repo dir here instead, and letting tags mount
    # their own files over it, is what produced `mount: read-only file system` at
    # container start — docker cannot create a mountpoint inside a read-only mount.
    add_docker_mount(state_commands_dir(inst_id.state_dir),
                     f"{CLAUDE_CONFIG_IN_CONTAINER}/commands:{RO_MOUNT_OPTION}")
    for source, target in DOCKER_BASE_MOUNTS.items():
        add_docker_mount(source, target)


def ensure_image(inst: Instance) -> str:
    """Build the instance's image stack bottom-up with plain `docker build`
    and return the final image tag. The base image builds from
    paths.BASE_DOCKERFILE; each subsequent step comes from the instance's
    build_steps (profession Dockerfiles + layer-bearing specialties), with
    PARENT_IMAGE pointing at the prior step's tag so each Dockerfile's
    `FROM ${PARENT_IMAGE}` resolves to a freshly-built parent. Each step
    forwards only the build-args its own `tag.docker` names (values from the
    staged container env), so one layer's args never surface in another's
    build. `--network=host` because BuildKit's own bridge has had DNS
    failures resolving curl/apt repo hosts on some setups. The build context
    is always the repo root."""
    tag = image_tag([])
    print(BUILDING_STEP.format(step="base", target=tag))
    docker_subprocess([
        "build", "--network=host", "-f", str(BASE_DOCKERFILE), "-t", tag,
        *build_arg_flags((str(ContainerEnvKey.SOFTWARE_STACK_REFRESH),)),
        str(DOCKERIZED_CLAUDE_ROOT),
    ])
    names: list[str] = []
    for name, dockerfile, contribution in inst.build_steps:
        parent = tag
        names.append(name)
        tag = image_tag(names)
        print(BUILDING_STEP.format(step=name, target=tag))
        docker_subprocess([
            "build", "--network=host", "-f", str(dockerfile), "-t", tag,
            "--build-arg", f"PARENT_IMAGE={parent}",
            *build_arg_flags(contribution.build_arg_forward if contribution else ()),
            str(DOCKERIZED_CLAUDE_ROOT),
        ])
    return tag


def prompt_install_failures(image: str, instance: str) -> None:
    """Read INSTALL_FAILURES_LOG_IN_CONTAINER from the final image; if it's
    non-empty, surface the failed tool names as a press-any-key prompt (so
    Claude Code's TUI takeover doesn't immediately clobber the warning).
    No-op when the file is missing (no [code] step in the stack) or empty
    (all installs succeeded). Self-contained: the prompt copy lives in
    template_code/docker_prompts and the rendering goes through prompt_keypress;
    nothing flows back to the caller. Uses `docker run --rm --entrypoint cat`
    for the one-shot read — one extra subprocess per launch (~few hundred ms
    after a warm image cache). Called by run.py between ensure_image and
    run_container so the list reflects the build that just finished.

    No-op on dry-run: ensure_image built nothing, so the only readable log
    would be a stale one from a previous real build — spinning up a real
    container to surface stale warnings would break dry-run's "project,
    don't touch" contract."""
    if _dry_run:
        return
    result = shell_capture(
        "docker", "run", "--rm", "--entrypoint", "cat",
        image, str(INSTALL_FAILURES_LOG_IN_CONTAINER),
    )
    if result.returncode != 0:
        return
    failures = sorted({line.strip() for line in result.stdout.splitlines() if line.strip()})
    if not failures:
        return
    prompt_keypress(
        header=INSTALL_FAILURES_HEADER.format(failures=", ".join(failures)),
        body=[line.format(instance=instance) for line in INSTALL_FAILURES_BODY],
    )


def effort_args(conf: dict[str, str], claude_args: list[str]) -> list[str]:
    """CLI args pinning the session's effort to the conf's
    CLAUDE_CODE_EFFORT_LEVEL (e.g. ["--effort", "max"]), or [] when the conf
    doesn't set a level or the user passed their own --effort through
    (theirs wins — both the `--effort max` and `--effort=max` forms count).

    Why a CLI flag when the same value already ships as a -e env var: on
    newly-launched models (Opus 4.7/4.8, Fable 5) Claude Code pins a fresh
    interactive session's effort to the model's launch default ("high") and
    treats CLAUDE_CODE_EFFORT_LEVEL as a session-only override it neither
    persists nor reflects in the /model UI — its pin logic explicitly checks
    argv for --effort as the user's confirmation. Passing the documented
    flag is the supported way to declare the level so the session both runs
    at it and reports it."""
    effort = conf.get("CLAUDE_CODE_EFFORT_LEVEL")
    if not effort or any(a == "--effort" or a.startswith("--effort=") for a in claude_args):
        return []
    return ["--effort", effort]


def run_cluster_container(session: str, image: str,
                          mounts: Iterable[tuple[str, str]],
                          entrypoint: str) -> None:
    """The cluster variant of `run_container` — deliberately a sibling here
    rather than a reimplementation in the cluster package (the plan's recorded
    decision: composing a `docker run` stays with the code that owns it).

    Much simpler than the instance path ON PURPOSE, and the absences are the
    contract, not oversights:
      - no per-tag cap_add / entrypoint chain / env forwards — the launch
        REFUSES members whose tags carry container-level docker features
        (launching.refusal), so there is nothing to assemble;
      - no engine conf at container level — each member's model/effort rides
        its own tmux window's env (the per-pane `-e` property that chose tmux);
      - no staged global mounts — the cluster assembles its own mount PAIRS
        (pairs, not a dict: the shared credentials file is the SOURCE of one
        mount per member, so sources repeat legally).
    The entrypoint is the generated cluster script (tmux session, one window
    per member, the shell window last); it is the container's PID 1 and holds
    the container open across detaches exactly like the solo muxer script."""
    container_name = f"{CONTAINER_NAME_PREFIX}cluster-{session}"
    set_terminal_title(f"cluster {session}")
    docker_subprocess(
        ["run", "--rm", "-it", "--name", container_name, "-e", "TERM",
         "--entrypoint", entrypoint]
        + [arg for source, target in mounts
           for arg in ("-v", f"{source}:{target}")]
        + container_env_args()   # the always-on -e set (BASH_ENV, status line)
        + [image])


def run_container(inst: Instance, image: str, claude_args: list[str], resume_flag: list[str],
                  *, interactive: bool = True, print_prompt: str | None = None,
                  stream_renderer: Callable[[Iterable[str]], None] | None = None) -> None:
    """Assemble and exec the final `docker run`. By the time we get here
    every bind-mount has been staged via add_docker_mount (base set,
    per-instance workspace/state, tag.docker mounts, [code] caches, skills,
    optional creds) — flatten _docker_mounts into `-v` flags inline. The
    active tags' contributions supply cap_add / entrypoint / env forwards;
    the engine conf and specialty claude_args ride the Instance. On a
    non-zero container return, docker_subprocess sys.exits with that code;
    on zero, returns normally and the __main__ unwind exits 0. The image
    itself is built upstream by ensure_image (called from run.py:launch
    before this).

    `interactive` (default True) allocates a TTY (`-it`) for the normal
    interactive Claude Code session. The quickie tool passes
    `interactive=False` + `print_prompt="<question>"` to run one-shot print
    mode (`claude -p "<question>"`): no TTY (so it works piped / from a
    script), the container exits after the answer. `-p` leads the claude argv
    so it reads as `claude -p "<q>" [flags]`.

    `stream_renderer`, when given (quickie's stream-json path), pipes docker's
    stdout line-by-line through it instead of inheriting the terminal, so the
    renderer can show progress and stream the answer; without it, docker keeps
    the terminal as before. The caller supplies the matching claude flags
    (`--output-format stream-json …`) via `claude_args`.

    {firewall} coordination: block on Phase 1 (critical Anthropic DNS) to
    get the initial WHITELIST_ADDRESSES — exiting with the worker's one-line
    message if a critical domain terminally failed to resolve — then spawn
    the firewall updater daemon thread BEFORE `docker run` so it can drain
    Phase 2 results into the running container's iptables via `docker exec`
    while Claude Code starts up. Both steps no-op when {firewall} is off
    (no resolution was started). `--name` is set explicitly to a
    deterministic string so the updater knows where to point.

    `-e TERM` is passed bare — docker forwards the value from the launcher's
    own terminal environment."""
    contributions = inst.docker_contributions
    set_terminal_title(inst.instance)
    # Phase 1 await: block for critical Anthropic addresses, stage them as the
    # initial WHITELIST_ADDRESSES. Phase 2 (rest of the whitelist) drains via
    # the updater thread spawned below.
    if is_critical_pending():
        print(FIREWALL_WAITING, flush=True)
    # A terminally-failed critical resolve surfaces as the phase-1 worker's
    # RuntimeError — exit with its message, not a raw traceback.
    addresses = call_or_exit(wait_for_critical_addresses, exceptions=RuntimeError)
    if addresses is not None:
        stage_container_env(ContainerEnvKey.WHITELIST_ADDRESSES, " ".join(addresses))
        if (selftest := selftest_address()) is not None:
            stage_container_env(ContainerEnvKey.FIREWALL_SELFTEST_ADDR, selftest)
    container_name = f"{CONTAINER_NAME_PREFIX}{inst.instance}"
    # {muxer}: the agent runs inside a multiplexer, so the command becomes a
    # generated startup script instead of claude's own argv. Assembled here
    # because this is where that argv is known, and only for an interactive
    # launch — quickie's print mode has no terminal to split.
    agent_argv = (
        ["claude"]
        + effort_args(inst.conf, claude_args)
        + resume_flag
        + list(inst.claude_args)
        + claude_args
    )
    muxed = interactive and inst.is_muxer
    if muxed:
        # {muxer} is the chain's TERMINAL link: the agent's argv is baked into the
        # generated script (quoting it through two more shells is where a flag
        # value with spaces would come apart), so nothing follows it.
        from .cluster import solo
        solo.install_launcher(inst, tuple(agent_argv))

    entry_flags, inner_links = entrypoint_chain(contributions)
    # What the container is actually told to run, after the image name:
    #   no wrapper      -> the agent's flags (the image ENTRYPOINT is `claude`)
    #   wrapper(s)      -> the remaining links, then the agent command explicitly
    #   ...ending in {muxer} -> nothing more; its script carries the agent itself
    if muxed:
        command = inner_links
    elif entry_flags:
        command = inner_links + agent_argv
    else:
        command = agent_argv[1:]        # `claude` comes from the image ENTRYPOINT

    # Spawn the updater BEFORE docker_subprocess (which blocks for the
    # container's lifetime). No-op for non-{firewall} launches.
    start_firewall_updater(container_name)
    args = (
        ["run", "--rm", *(["-it"] if interactive else []), "--name", container_name, "-e", "TERM"]
        + [f"--cap-add={cap}" for c in contributions for cap in c.cap_add]
        + entry_flags
        + [arg for src, tgt in _docker_mounts.items() for arg in ("-v", f"{src}:{tgt}")]
        + container_env_args()             # always-on -e flags (status line, BASH_ENV, cred tokens)
        + conf_env_args(inst.conf)         # -e flags setting each engine-conf key=value in the container
        + env_forward_flags(contributions)  # tag-conditional -e flags (WHITELIST_ADDRESSES)
        + [image]
        + (["-p", print_prompt] if print_prompt is not None else [])   # one-shot print mode (quickie)
        + command                          # see the chain assembly above
    )
    if stream_renderer is not None:
        docker_stream_subprocess(args, stream_renderer)
    else:
        docker_subprocess(args)
