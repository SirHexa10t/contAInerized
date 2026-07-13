"""Docker-side launcher orchestration — everything between "we picked an agent"
and `docker compose run`. The image-build chain (ensure_image), the bind-mount
accumulator that flattens into `-v` flags (set_container_mounts +
add_docker_mount + mount_target_is_staged), small `docker` CLI wrappers
(require_docker, detect_docker_gid, docker_check_running_subprocess,
wait_for_container_running, docker_exec_root_subprocess,
docker_check_any_agent_running_subprocess, docker_compose_subprocess),
the image-chain naming helpers (chain_image_tag, chain_compose_files), the
post-build install-failure surfacing (prompt_install_failures), and the
compose invocation itself (run_compose).

Sister accumulator lives in compose_env: `_compose_env` for env-var staging.
This module holds:
  - _docker_mounts: {source: "target[:ro]"} — staged via add_docker_mount,
    flattened inline by run_compose into `-v` flags.

Imports from paths (filesystem constants), claude_code_config (terminal title),
compose_env (env staging + container_env_args + conf_env_args + subprocess_env),
and network (the {auto}-mode firewall coordination hooks). agent_modifiers_handler
imports add_docker_mount + docker_check_any_agent_running_subprocess +
detect_docker_gid from here; run.py is the top-level consumer.
"""

import shutil
import subprocess
import sys
import time
from pathlib import Path

from .claude_code_config import set_terminal_title
from .compose_env import (
    ComposeEnvKey, conf_env_args, container_env_args, stage_compose_env,
    subprocess_env,
)
from .network import is_critical_pending, start_firewall_updater, wait_for_critical_addresses
from .paths import (
    CLAUDE_CONFIG_IN_CONTAINER, COMPOSE_FILE_PATH, DEFAULT_WORKSPACE,
    DOCKER_BASE_MOUNTS, FIREWALL_DONE_IN_CONTAINER,
    INSTALL_FAILURES_LOG_IN_CONTAINER, compose_layer_path,
)
from .structs import InstanceIdentity
from .template_code.docker_prompts import (
    AUTO_FIREWALL_WAITING, BUILDING_STEP, INSTALL_FAILURES_BODY, INSTALL_FAILURES_HEADER,
)
from .utils import exit_if_missing, prompt_keypress, shell_capture, shell_returncode


# ============================================================
# Docker volume accumulator
# ============================================================
# Every bind-mount for `docker compose run` flows through this dict. set_container_mounts
# stages the always-on set (paths.DOCKER_BASE_MOUNTS + the per-instance workspace/state dirs);
# agent_modifiers_handler's tag/mode handlers stage chain-step contributions ([code] caches);
# user_additions stages skills + optional creds. run_compose flattens the dict into
# `-v src:tgt[:ro]` flags appended to the docker compose command. Mirror of
# compose_env's `_compose_env` / stage_compose_env pattern — declarations flow
# one way, emission stays in this module. compose.auto.yml's two
# ${...}-substituted mounts are the only bind-mounts that still travel via
# YAML (their ComposeEnvKey constants live in compose_env).

_docker_mounts: dict[str, str] = {}   # {source_path_str: "target_path[:ro]"} — source uniquely identifies a mount across our callers


def add_docker_mount(source: Path | str, target: Path | str) -> None:
    """Stage a bind-mount for the upcoming `docker compose run` invocation. Any
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
# Image-chain naming
# ============================================================

def chain_image_tag(chain: list[str]) -> str:
    """The docker image tag for a chain. ['base'] → 'claude-agents:base'.
    ['base', 'code', 'auto'] → 'claude-agents:code.auto' (lowercase to match
    the lowercase compose/Dockerfile filenames)."""
    if len(chain) == 1:
        return "claude-agents:base"
    return "claude-agents:" + ".".join(step.lower() for step in chain[1:])


def chain_compose_files(chain: list[str]) -> list[str]:
    """The compose `-f <path>` arg list for a chain. Always includes compose.yml;
    adds compose.<step>.yml (lowercased) for each non-base step in order."""
    args = ["-f", str(COMPOSE_FILE_PATH)]
    for step in chain[1:]:
        args += ["-f", str(compose_layer_path(step))]
    return args


# ============================================================
# Docker subprocess helpers
# ============================================================
# Every docker-CLI touchpoint outside orchestration lives here: the PATH
# presence check (require_docker), the read-only probes used by firewall
# coordination + cache pruning (detect_docker_gid,
# docker_check_running_subprocess, wait_for_container_running,
# docker_exec_root_subprocess, docker_check_any_agent_running_subprocess),
# and the `docker compose` invocation wrapper (docker_compose_subprocess)
# used by ensure_image / run_compose below in the orchestration section.
# CONTAINER_NAME_PREFIX is the one place the per-launch container name format
# is defined — run_compose builds container names from it, and
# docker_check_any_agent_running_subprocess filters `docker ps` by the same
# prefix; keeping them consistent is a one-line change here.

CONTAINER_NAME_PREFIX = "claude-code_"   # prefix for every per-launch container name (run_compose) and the filter used to detect a running agent (docker_check_any_agent_running_subprocess)


# ============================================================
# Dry-run flag
# ============================================================
# Module-level toggle gating docker_compose_subprocess's actual subprocess
# invocation. Set once at startup from run.py:launch via set_dry_run(); the
# default False means "real run" so callers that import this module without
# going through launch() (tests, audit) behave normally. The flag lives here
# rather than threaded through every function because the only operation it
# affects is the docker compose call itself — every other orchestration step
# (mount staging, env staging, firewall coordination, banner printing)
# happens identically in both modes, which is what makes --dry-run a
# faithful projection of a real run.

_dry_run = False


def set_dry_run(value: bool) -> None:
    """Set the module-level dry-run flag. Called from run.py:launch after CLI
    parsing. docker_compose_subprocess checks this to gate its underlying
    subprocess.call — every surrounding step still runs so the user sees an
    accurate projection of what a real run would do."""
    global _dry_run
    _dry_run = value


def require_docker() -> None:
    """Exit early with a clean message if `docker` isn't on PATH. Run.py calls this
    at startup so a missing daemon surfaces as a one-liner instead of a deeper-down
    docker-compose traceback later."""
    exit_if_missing(shutil.which("docker"), "docker is required but was not found in PATH.")


def detect_docker_gid() -> str | None:
    """Return the host's docker group GID as a string, or None if no docker
    group exists (or `getent` is unavailable — e.g. non-Linux hosts). Used by
    agent_modifiers_handler._apply_dood to stage DOCKER_GID for Dockerfile.dood, so
    claude can read/write the bind-mounted /var/run/docker.sock."""
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
    False on timeout. `docker compose run` creates the container almost
    immediately but `docker inspect` returns 'not found' for a small window
    after — hence the poll. Used by the {auto}-mode firewall updater (in
    network._updater_worker) before it starts issuing `docker exec` calls.

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
    launches. Used by network._updater_worker between
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
    user. Used by the {auto}-mode firewall updater (in network._insert_iptables_accept)
    to inject iptables ACCEPT rules into the running container as Phase 2 DNS
    resolutions complete. Centralised here so privileged docker-exec calls
    have a single audit point."""
    return shell_capture("docker", "exec", "--user", "root", container_name, *cmd)


def docker_compose_subprocess(args: list[str]) -> None:
    """Run `docker compose <args>` with `subprocess_env()` overlaid; sys.exit
    with the return code on non-zero, return silently on success. On dry-run
    (set via set_dry_run), print what would have been invoked and return
    without touching subprocess — every surrounding orchestration step still
    runs, so dry-run projects accurately.

    Both real-run callers want the exit-on-failure shape: ensure_image needs
    to continue to the next chain step on success but die if any build fails;
    run_compose is the program's terminal — on success the unwind through
    launch() → __main__ exits the process with 0 naturally, equivalent to an
    explicit sys.exit(0). The "docker compose" prefix + the staged env are
    this codebase's universal compose-invocation pattern."""
    if _dry_run:
        print(f"  (dry-run: would invoke `docker compose {' '.join(args)}`)")
        return
    if (ret := shell_returncode("docker", "compose", *args, env=subprocess_env())) != 0:
        sys.exit(ret)


def docker_check_any_agent_running_subprocess() -> bool:
    """True if any container whose name starts with CONTAINER_NAME_PREFIX is
    currently running, OR if `docker ps` failed (conservative — treat the
    unknown state as 'might be running' so caller skips its cleanup). Used
    by agent_modifiers_handler.prune_caches as the 'is it safe to delete cache
    files' guard. Uses `bool(stdout.strip())` rather than `== "true"` because
    `--format={{.Names}}` outputs container names (one per line) — any
    non-empty output means matching containers exist."""
    r = shell_capture("docker", "ps", "--filter", f"name={CONTAINER_NAME_PREFIX}", "--format", "{{.Names}}")
    return r.returncode != 0 or bool(r.stdout.strip())


# ============================================================
# Orchestration
# ============================================================

def set_container_mounts(inst_id: InstanceIdentity) -> None:
    """Stage per-launch bind-mounts via add_docker_mount. Sister to set_container_env
    (bind-mounts vs env vars); both run sequentially in setup_state. Two layers:
    the per-instance pair (workspace → /workspace, state dir → /home/claude/.claude)
    derived from inst_id, plus the always-on DOCKER_BASE_MOUNTS from paths.py
    (whose target strings already carry any `:ro` suffix).

    Workspace fallback: if `inst_id.workspace` is None (stale map entry that
    survived all the upstream prompts, or a session constructed without a
    workspace), default to DEFAULT_WORKSPACE so the bind-mount still resolves
    to a real host directory rather than crashing the compose invocation."""
    add_docker_mount(inst_id.workspace or DEFAULT_WORKSPACE, "/workspace")
    add_docker_mount(inst_id.state_dir, CLAUDE_CONFIG_IN_CONTAINER)
    for source, target in DOCKER_BASE_MOUNTS.items():
        add_docker_mount(source, target)


def ensure_image(chain: list[str]) -> None:
    """Build each step in the chain sequentially. Each step's image is tagged
    according to chain_image_tag(chain[:i+1]); PARENT_IMAGE for non-base steps
    points to the prior step's tag so each Dockerfile's `FROM ${PARENT_IMAGE}`
    resolves to a freshly-built parent. Each build invocation uses only
    compose.yml + the step's own compose file (intermediates aren't included
    so their build-args don't surface in unrelated Dockerfile builds)."""
    prev_tag = None
    for i, step in enumerate(chain):
        target = chain_image_tag(chain[:i + 1])
        compose_files = chain_compose_files(["base"] if step == "base" else ["base", step])
        stage_compose_env(ComposeEnvKey.TARGET_IMAGE, target)
        if prev_tag:
            stage_compose_env(ComposeEnvKey.PARENT_IMAGE, prev_tag)
        print(BUILDING_STEP.format(step=step, target=target))
        docker_compose_subprocess(compose_files + ["build"])
        prev_tag = target


def prompt_install_failures(chain: list[str], instance: str) -> None:
    """Read INSTALL_FAILURES_LOG_IN_CONTAINER from the final chain image; if
    it's non-empty, surface the failed tool names as a press-any-key prompt
    (so Claude Code's TUI takeover doesn't immediately clobber the warning).
    No-op when the file is missing (no [code] step in the chain) or empty
    (all installs succeeded). Self-contained: the prompt copy lives in
    template_code/docker_prompts and the rendering goes through prompt_keypress;
    nothing flows back to the caller. Uses `docker run --rm --entrypoint cat`
    for the one-shot read — one extra subprocess per launch (~few hundred ms
    after a warm image cache). Called by run.py between ensure_image and
    run_compose so the list reflects the build that just finished.

    No-op on dry-run: ensure_image built nothing, so the only readable log
    would be a stale one from a previous real build — spinning up a real
    container to surface stale warnings would break dry-run's "project,
    don't touch" contract."""
    if _dry_run:
        return
    result = shell_capture(
        "docker", "run", "--rm", "--entrypoint", "cat",
        chain_image_tag(chain), str(INSTALL_FAILURES_LOG_IN_CONTAINER),
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


def run_compose(chain: list[str], instance: str, claude_args: list[str], resume_flag: list[str], conf: dict[str, str]) -> None:
    """Set TARGET_IMAGE so compose's `image:` substitutes to the chain output,
    set the terminal title, then exec `docker compose run`. By the time we
    get here every bind-mount has been staged via add_docker_mount (base
    set, per-instance workspace/state, [code] caches, skills, optional
    creds) — flatten _docker_mounts into `-v` flags inline. On a non-zero
    container return, docker_compose_subprocess sys.exits with that code;
    on zero, returns normally and the __main__ unwind exits 0. The chain
    images themselves are built upstream by ensure_image (called from
    run.py:launch before this).

    {auto}-mode firewall coordination: block on Phase 1 (critical Anthropic
    DNS) to get the initial WHITELIST_ADDRESSES, then spawn the firewall
    updater daemon thread BEFORE `docker_compose_subprocess` so it can drain Phase 2
    results into the running container's iptables via `docker exec` while
    Claude Code starts up. `--name` is set explicitly to a deterministic
    string so the updater knows where to point — `docker compose run` would
    otherwise generate a random suffix and we'd have no way to find it from
    outside without polling `docker compose ps`."""
    stage_compose_env(ComposeEnvKey.TARGET_IMAGE, chain_image_tag(chain))
    compose_args = chain_compose_files(chain)
    set_terminal_title(instance)
    # Phase 1 await: block for critical Anthropic addresses, stage them as the
    # initial WHITELIST_ADDRESSES. Phase 2 (rest of the whitelist) is still
    # resolving in the background; the updater thread (spawned below) handles
    # it via `docker exec` once the container is up.
    if is_critical_pending():
        print(AUTO_FIREWALL_WAITING, flush=True)
    if (addresses := wait_for_critical_addresses()) is not None:
        stage_compose_env(ComposeEnvKey.WHITELIST_ADDRESSES, " ".join(addresses))
    container_name = f"{CONTAINER_NAME_PREFIX}{instance}"
    # Spawn the updater BEFORE docker_compose_subprocess (which blocks for the container's
    # lifetime) — the daemon thread will see the container come up shortly and
    # start draining Phase 2 results onto iptables. No-op for non-{auto} launches.
    start_firewall_updater(container_name)
    args = (
        compose_args + ["run", "--rm", "-it", "--name", container_name]
        + [arg for src, tgt in _docker_mounts.items() for arg in ("-v", f"{src}:{tgt}")]
        + container_env_args()    # per-key -e flags from CONTAINER_ENV_FORWARDS
        + conf_env_args(conf)     # -e flags setting each per-agent conf key=value in the container
        + ["claude-code"]
        + resume_flag             # present if a resumed session
        + claude_args             # leftover argv (unrecognised flags + unresolved positional) → claude
    )
    docker_compose_subprocess(args)
