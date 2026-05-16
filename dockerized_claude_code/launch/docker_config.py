"""Docker-side launcher orchestration — everything between "we picked an agent"
and `docker compose run`. The image-build chain (ensure_image), the bind-mount
accumulator that flattens into `-v` flags (set_container_mounts +
add_docker_mount), small `docker` CLI wrappers (require_docker,
detect_docker_gid, wait_for_container_running, docker_exec_root,
any_agent_container_running), the image-chain naming helpers (chain_image_tag,
chain_compose_files), and the compose invocation itself (run_compose).

Sister accumulator lives in compose_env: `_compose_env` for env-var staging.
This module holds:
  - _docker_mounts: {source: "target[:ro]"} — staged via add_docker_mount,
    flattened inline by run_compose into `-v` flags. staged_mounts() exposes
    read-only access for the launch banner.

Imports from paths (filesystem constants), claude_code_config (terminal title),
compose_env (env staging + container_env_args + conf_env_args + subprocess_env),
and network (the {auto}-mode firewall coordination hooks). agent_composition
imports add_docker_mount + any_agent_container_running + detect_docker_gid
from here; run.py is the top-level consumer.
"""

import shutil
import subprocess
import sys
import time

from .claude_code_config import set_terminal_title
from .compose_env import (
    ComposeEnvKey, conf_env_args, container_env_args, stage_compose_env,
    subprocess_env,
)
from .network import is_critical_pending, start_firewall_updater, wait_for_critical_addresses
from .paths import (
    CLAUDE_CONFIG_IN_CONTAINER, COMPOSE_FILE_PATH, DEFAULT_WORKSPACE,
    DOCKER_BASE_MOUNTS, compose_layer_path,
)


# ============================================================
# Docker volume accumulator
# ============================================================
# Every bind-mount for `docker compose run` flows through this dict. set_container_mounts
# stages the always-on set (paths.DOCKER_BASE_MOUNTS + the per-instance workspace/state dirs);
# agent_composition's tag/mode handlers stage chain-step contributions ([prog] caches);
# user_additions stages skills + optional creds. run_compose flattens the dict into
# `-v src:tgt[:ro]` flags appended to the docker compose command. Mirror of
# compose_env's `_compose_env` / stage_compose_env pattern — declarations flow
# one way, emission stays in this module. compose.auto.yml's two
# ${...}-substituted mounts are the only bind-mounts that still travel via
# YAML (their ComposeEnvKey constants live in compose_env).

_docker_mounts: dict[str, str] = {}   # {source_path_str: "target_path[:ro]"} — source uniquely identifies a mount across our callers


def add_docker_mount(source, target) -> None:
    """Stage a bind-mount for the upcoming `docker compose run` invocation. Any
    docker access-mode suffix (`:ro`, also `:z`/`:Z`, `:cached`/`:delegated`,
    propagation modes) is the caller's responsibility — bake it into target
    when needed. Both args coerce to str at this boundary so callers can pass
    Path objects without thinking about it."""
    _docker_mounts[str(source)] = str(target)


def staged_mounts() -> dict[str, str]:
    """The {source: target[:ro]} dict of bind-mounts staged so far. Read-only —
    callers should not mutate. Provided so the launch banner can introspect
    what's about to be mounted without having per-category counts threaded
    through every layer above."""
    return _docker_mounts


# ============================================================
# Image-chain naming
# ============================================================

def chain_image_tag(chain: list[str]) -> str:
    """The docker image tag for a chain. ['base'] → 'claude-agents:base'.
    ['base', 'prog', 'auto'] → 'claude-agents:prog.auto' (lowercase to match
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


def require_docker() -> None:
    """Exit early with a clean message if `docker` isn't on PATH. Run.py calls this
    at startup so a missing daemon surfaces as a one-liner instead of a deeper-down
    docker-compose traceback later."""
    if shutil.which("docker") is None:
        sys.exit("docker is required but was not found in PATH.")


# ============================================================
# Docker subprocess helpers
# ============================================================
# Every direct invocation of the `docker` CLI flows through this section
# (the `docker compose build/run` calls in ensure_image / run_compose live
# below in the orchestration section since they're tied to per-launch state).
# CONTAINER_NAME_PREFIX is the one place the per-launch container name format
# is defined — run_compose builds container names from it, and
# any_agent_container_running filters `docker ps` by the same prefix; keeping
# them consistent is a one-line change here.

CONTAINER_NAME_PREFIX = "claude-code_"   # prefix for every per-launch container name (run_compose) and the filter used to detect a running agent (any_agent_container_running)


def detect_docker_gid() -> str | None:
    """Return the host's docker group GID as a string, or None if no docker
    group exists (or `getent` is unavailable — e.g. non-Linux hosts). Used by
    agent_composition._apply_dood to stage DOCKER_GID for Dockerfile.dood, so
    claude can read/write the bind-mounted /var/run/docker.sock."""
    try:
        result = subprocess.run(
            ["getent", "group", "docker"],
            capture_output=True, text=True, check=False,
        )
    except FileNotFoundError:
        return None  # no getent (e.g., not Linux)
    if result.returncode == 0 and result.stdout.strip():
        return result.stdout.strip().split(":")[2]
    return None


def wait_for_container_running(container_name: str, timeout_seconds: float = 10) -> bool:
    """Poll `docker inspect` until the named container reports State.Running==true
    or `timeout_seconds` passes. Returns True if the container came up in time,
    False on timeout. `docker compose run` creates the container almost
    immediately but `docker inspect` returns 'not found' for a small window
    after — hence the poll. Used by the {auto}-mode firewall updater (in
    network._updater_worker) before it starts issuing `docker exec` calls."""
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        r = subprocess.run(
            ["docker", "inspect", "--format={{.State.Running}}", container_name],
            capture_output=True, text=True,
        )
        if r.returncode == 0 and r.stdout.strip() == "true":
            return True
        time.sleep(0.1)
    return False


def docker_exec_root(container_name: str, *cmd: str) -> subprocess.CompletedProcess:
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
    return subprocess.run(
        ["docker", "exec", "--user", "root", container_name, *cmd],
        capture_output=True, text=True,
    )


def any_agent_container_running() -> bool:
    """True if any container whose name starts with CONTAINER_NAME_PREFIX is
    currently running, OR if `docker ps` failed (conservative — treat the
    unknown state as 'might be running' so caller skips its cleanup). Used
    by agent_composition.prune_caches as the 'is it safe to delete cache
    files' guard."""
    r = subprocess.run(
        ["docker", "ps", "--filter", f"name={CONTAINER_NAME_PREFIX}", "--format", "{{.Names}}"],
        capture_output=True, text=True,
    )
    return r.returncode != 0 or bool(r.stdout.strip())


# ============================================================
# Orchestration
# ============================================================

def set_container_mounts(inst_id) -> None:
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
        print(f"  Building {step} → {target}...")
        ret = subprocess.call(["docker", "compose"] + compose_files + ["build"], env=subprocess_env())
        if ret != 0:
            sys.exit(ret)
        prev_tag = target


def run_compose(chain: list[str], instance: str, claude_args: list[str], resume_flag: list[str], conf: dict) -> None:
    """Build each image in the chain, set TARGET_IMAGE so compose's `image:`
    substitutes to the chain output, set the terminal title, then exec
    `docker compose run`. By the time we get here every bind-mount has been
    staged via add_docker_mount (base set, per-instance workspace/state,
    [prog] caches, skills, optional creds) — flatten _docker_mounts into `-v`
    flags inline. sys.exits with the container's return code.

    {auto}-mode firewall coordination: block on Phase 1 (critical Anthropic
    DNS) to get the initial WHITELIST_ADDRESSES, then spawn the firewall
    updater daemon thread BEFORE `subprocess.call` so it can drain Phase 2
    results into the running container's iptables via `docker exec` while
    Claude Code starts up. `--name` is set explicitly to a deterministic
    string so the updater knows where to point — `docker compose run` would
    otherwise generate a random suffix and we'd have no way to find it from
    outside without polling `docker compose ps`."""
    ensure_image(chain)
    stage_compose_env(ComposeEnvKey.TARGET_IMAGE, chain_image_tag(chain))
    compose_args = chain_compose_files(chain)
    set_terminal_title(instance)
    # Phase 1 await: block for critical Anthropic addresses, stage them as the
    # initial WHITELIST_ADDRESSES. Phase 2 (rest of the whitelist) is still
    # resolving in the background; the updater thread (spawned below) handles
    # it via `docker exec` once the container is up.
    if is_critical_pending():
        print("  Waiting for critical {auto}-mode firewall addresses...", flush=True)
    if (addresses := wait_for_critical_addresses()) is not None:
        stage_compose_env(ComposeEnvKey.WHITELIST_ADDRESSES, " ".join(addresses))
    container_name = f"{CONTAINER_NAME_PREFIX}{instance}"
    # Spawn the updater BEFORE subprocess.call (which blocks for the container's
    # lifetime) — the daemon thread will see the container come up shortly and
    # start draining Phase 2 results onto iptables. No-op for non-{auto} launches.
    start_firewall_updater(container_name)
    cmd = (
        ["docker", "compose"] + compose_args + ["run", "--rm", "-it", "--name", container_name]
        + [arg for src, tgt in _docker_mounts.items() for arg in ("-v", f"{src}:{tgt}")]
        + container_env_args()    # per-key -e flags from CONTAINER_ENV_FORWARDS / CONTAINER_ENV_FIXED
        + conf_env_args(conf)     # -e flags setting each per-agent conf key=value in the container
        + ["claude-code"]
        + resume_flag             # present if a resumed session
        + claude_args             # leftover argv (unrecognised flags + unresolved positional) → claude
    )
    sys.exit(subprocess.call(cmd, env=subprocess_env()))
