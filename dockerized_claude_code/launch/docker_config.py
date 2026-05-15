"""Docker-side launcher orchestration — everything between "we picked an agent"
and `docker compose run`. The image-build chain (ensure_image), the env-var
accumulator that compose reads at substitution time (set_container_env +
stage_compose_env), the bind-mount accumulator that flattens into `-v` flags
(set_container_mounts + add_docker_mount), the status-line label builder, and
the compose invocation itself (run_compose).

Two sister accumulators live here:
  - _compose_env: {key: value} — staged via stage_compose_env, overlaid on
    host env at subprocess time by _subprocess_env (values coerced to str at
    that boundary). The launcher never writes to its own os.environ; swapping
    transport (.env file, `-e` flags, …) is a one-line change to _subprocess_env.
  - _docker_mounts: {source: "target[:ro]"} — staged via add_docker_mount,
    flattened inline by run_compose into `-v` flags. staged_mounts() exposes
    read-only access for the launch banner.

Compose env-var keys are members of the `ComposeEnvKey` enum (a str-subclass
enum so members work transparently in dict keys, f-strings, and subprocess
env). External callers import `ComposeEnvKey` and pass `ComposeEnvKey.TARGET_IMAGE`
etc. to stage_compose_env — typos surface as AttributeError, and the set is
grepp-able in one place.

Imports from paths (filesystem constants — none of them collide with env-key
names now that ComposeEnvKey scopes the latter) and file_access (the optional-
creds primitives). agent_composition imports stage_compose_env / add_docker_mount
plus ComposeEnvKey from here; run.py is the top-level consumer.
"""

import os
import shutil
import subprocess
import sys
import time
from datetime import date
from enum import Enum

from .claude_code_config import build_status_line, set_terminal_title
from .file_access import optional_cred_tokens, present_optional_cred_services
from .network import is_critical_pending, start_firewall_updater, wait_for_critical_addresses
from .paths import (
    CLAUDE_CONFIG_IN_CONTAINER, CLAUDE_HOME_IN_CONTAINER, COMPOSE_FILE_PATH,
    DOCKER_BASE_MOUNTS, DOCKERIZED_CLAUDE_ROOT, OPTIONAL_CREDS_MOUNTS,
    OPTIONAL_CREDS_TOKEN_ENV_VARS, compose_layer_path,
)


# ============================================================
# Compose env-var keys
# ============================================================
# Every static compose / container env-var name the launcher stages or
# emits, collected here so the set is grepp-able and IDE-completable.
# Members are str-subclasses (via `class X(str, Enum)`), so each member
# transparently works as a dict key, in subprocess env, and as a `==`
# comparator against a string. The `__str__` override makes f-strings emit
# the value (`"TARGET_IMAGE"`), not the enum repr (`"ComposeEnvKey.TARGET_IMAGE"`)
# — that's important for the `-e KEY=VALUE` flag emission in run_compose.
#
# Dynamic env-var names (per-service tokens from OPTIONAL_CREDS_TOKEN_ENV_VARS,
# build-arg INSTALL_<TOOL> flags) are not enumerated here — they're derived
# at run time from paths-side configuration.

class ComposeEnvKey(str, Enum):
    # Image build chain — driven into compose-YAML ${...} substitution
    TARGET_IMAGE           = "TARGET_IMAGE"            # compose.yml `image:` — current step's tag (set per chain step + once more in run_compose)
    PARENT_IMAGE           = "PARENT_IMAGE"            # compose.<step>.yml `FROM ${PARENT_IMAGE}` — prior step's tag; not set on base
    SOFTWARE_STACK_REFRESH = "SOFTWARE_STACK_REFRESH"  # weekly cache-buster for curl-piped Dockerfile installs (uv, rich-cli, Claude Code, rustup)
    # Per-instance identity
    AGENT_NAME             = "AGENT_NAME"              # agent's clean name — substituted into compose.yml's `container_name:`
    # Mode-driven keys
    WHITELIST_ADDRESSES    = "WHITELIST_ADDRESSES"     # {auto}-mode firewall list of pre-resolved `<ip>[:port]` / `<cidr>[:port]` tokens (space-joined) — read by init-firewall.sh inside the container
    DOCKER_GID             = "DOCKER_GID"              # host docker group GID — Dockerfile.dood build-arg for /var/run/docker.sock access
    # Build/launch wiring (compose-YAML ${...} substitution)
    DOCKERIZED_CLAUDE_ROOT = "DOCKERIZED_CLAUDE_ROOT"  # repo root — `context: ${DOCKERIZED_CLAUDE_ROOT}` in every build block
    # Container-side env vars (emitted by run_compose as `-e KEY=VALUE` flags)
    AGENT_STATUS_LINE      = "AGENT_STATUS_LINE"       # pre-styled ANSI status line at the bottom of Claude Code
    BASH_ENV               = "BASH_ENV"                # path to the bashrc that non-interactive bash sources at startup

    def __str__(self):                                 # `f"{key}"` → "TARGET_IMAGE", not "ComposeEnvKey.TARGET_IMAGE"
        return self.value


# ============================================================
# Container env-var emissions — appended as `-e` flags in run_compose
# ============================================================
# Container env-vars are emitted as `-e KEY=VALUE` flags on the `docker
# compose run` command by run_compose, rather than declared in compose.yml's
# `environment:` block. Same shape as the bind-mount accumulation pattern in
# this module: declarative wiring lives in Python, with compose YAMLs reserved
# for the build graph + the build-context root substitution.
#
# CONTAINER_ENV_FORWARDS holds keys whose values are already in `_compose_env`
# (staged by stage_compose_env elsewhere — AGENT_STATUS_LINE from set_container_env,
# and the optional-creds token vars from token_env_dict). Keys absent from
# `_compose_env` at run_compose time are silently skipped — that's how the
# JIRA_API_TOKEN passthrough stays conditional on optional_creds/jira/token
# being present.
#
# CONTAINER_ENV_FIXED holds key→value pairs with constant in-container values
# (BASH_ENV). Emitted unconditionally.
#
# TERM is intentionally *not* here — it's purely shell-inherited (whatever the
# user's terminal sets), so compose.yml's `environment: [- TERM]` pass-through
# is the natural fit.

CONTAINER_ENV_FORWARDS = (ComposeEnvKey.AGENT_STATUS_LINE, *OPTIONAL_CREDS_TOKEN_ENV_VARS.values())
CONTAINER_ENV_FIXED    = {ComposeEnvKey.BASH_ENV: f"{CLAUDE_HOME_IN_CONTAINER}/.bashrc"}


# ============================================================
# Compose env accumulator
# ============================================================

_compose_env = {}    # populated by set_container_env, ensure_image, run_compose, and the agent_composition mode handlers; read at subprocess invocation time


def stage_compose_env(key, value):
    """Buffer a single compose env-var entry (any value type — `_subprocess_env`
    coerces to str at the subprocess boundary). Pass one of the module-level
    UPPERCASE constants as the key. set_container_env writes its bulk batch
    directly via `_compose_env.update({...})` since it's in this module."""
    _compose_env[key] = value


def _subprocess_env():
    """Host env overlaid with the staged compose entries (values coerced to
    str at this boundary so the accumulator can hold Path/int/etc.) — passed
    as env= to every docker-compose subprocess."""
    return {**os.environ, **{k: str(v) for k, v in _compose_env.items()}}


# ============================================================
# Docker volume accumulator
# ============================================================
# Every bind-mount for `docker compose run` flows through this dict. set_container_mounts
# stages the always-on set (paths.DOCKER_BASE_MOUNTS + the per-instance workspace/state dirs);
# agent_composition's tag/mode handlers stage chain-step contributions ([prog] caches);
# user_additions stages skills + optional creds. run_compose flattens the dict into
# `-v src:tgt[:ro]` flags appended to the docker compose command. Mirror of the
# _compose_env / stage_compose_env pattern above — declarations flow one way, emission
# stays in this module. compose.auto.yml's two ${...}-substituted mounts are the only
# bind-mounts that still travel via YAML (their env-key constants live above).

_docker_mounts = {}   # {source_path_str: "target_path[:ro]"} — source uniquely identifies a mount across our callers


def add_docker_mount(source, target):
    """Stage a bind-mount for the upcoming `docker compose run` invocation. Any
    docker access-mode suffix (`:ro`, also `:z`/`:Z`, `:cached`/`:delegated`,
    propagation modes) is the caller's responsibility — bake it into target
    when needed. Both args coerce to str at this boundary so callers can pass
    Path objects without thinking about it."""
    _docker_mounts[str(source)] = str(target)


def staged_mounts():
    """The {source: target[:ro]} dict of bind-mounts staged so far. Read-only —
    callers should not mutate. Provided so the launch banner can introspect
    what's about to be mounted without having per-category counts threaded
    through every layer above."""
    return _docker_mounts


# ============================================================
# Image-chain naming
# ============================================================

def chain_image_tag(chain):
    """The docker image tag for a chain. ['base'] → 'claude-agents:base'.
    ['base', 'prog', 'auto'] → 'claude-agents:prog.auto' (lowercase to match
    the lowercase compose/Dockerfile filenames)."""
    if len(chain) == 1:
        return "claude-agents:base"
    return "claude-agents:" + ".".join(step.lower() for step in chain[1:])


def chain_compose_files(chain):
    """The compose `-f <path>` arg list for a chain. Always includes compose.yml;
    adds compose.<step>.yml (lowercased) for each non-base step in order."""
    args = ["-f", str(COMPOSE_FILE_PATH)]
    for step in chain[1:]:
        args += ["-f", str(compose_layer_path(step))]
    return args


def require_docker():
    """Exit early with a clean message if `docker` isn't on PATH. Run.py calls this
    at startup so a missing daemon surfaces as a one-liner instead of a deeper-down
    docker-compose traceback later."""
    if shutil.which("docker") is None:
        sys.exit("docker is required but was not found in PATH.")


def conf_env_args(conf):
    """Convert a per-agent `.conf` dict (from file_access.load_conf) into a
    list of `-e KEY=VALUE` args for `docker compose run`. Each conf entry becomes
    a runtime env var inside the container."""
    return [item for k, v in conf.items() for item in ("-e", f"{k}={v}")]


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


def detect_docker_gid():
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


def wait_for_container_running(container_name, timeout_seconds=10):
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


def docker_exec_root(container_name, *cmd):
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


def any_agent_container_running():
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
# Compose-env formatters for user-side contributions
# ============================================================
# These shape raw data the file_access layer discovered on disk into the
# {KEY: VALUE} dicts the compose env accumulator consumes. Bind-mount staging
# for the same user-side data goes through add_docker_mount in the caller
# (user_additions), not here.

def install_creds_flags(services):
    """`{INSTALL_<TOOL>: '0' | '1'}` dict for Dockerfile.prog's build-args.
    One entry per OPTIONAL_CREDS_MOUNTS service; value is '1' when the
    matching cred dir is present (in `services`), '0' otherwise. Dockerfile.prog
    branches on each flag to decide whether to install that CLI."""
    return {f"INSTALL_{name.upper()}": ("1" if name in services else "0")
            for name in OPTIONAL_CREDS_MOUNTS}


def token_env_dict(tokens):
    """`{<env_var>: <token_string>}` dict, translating `{service: token}` (from
    file_access.optional_cred_tokens) via OPTIONAL_CREDS_TOKEN_ENV_VARS. Each
    entry forwards a per-service token into the container as the env var the
    matching CLI expects."""
    return {OPTIONAL_CREDS_TOKEN_ENV_VARS[svc]: tok
            for svc, tok in tokens.items()
            if svc in OPTIONAL_CREDS_TOKEN_ENV_VARS}


# ============================================================
# Orchestration
# ============================================================

def set_container_env(inst_id):
    """Stage per-launch compose env vars in one bulk dict-update — called by run.py
    before docker compose build/run. Sister to set_container_mounts (env vars vs
    bind-mounts); both run sequentially in setup_state. Accepts any
    InstanceIdentity (or subclass); only reads .agent for the container name,
    plus what the status-line builder consumes."""
    _compose_env.update({
        ComposeEnvKey.SOFTWARE_STACK_REFRESH: date.today().strftime("%Y-W%W"),
        ComposeEnvKey.AGENT_NAME:             inst_id.agent,
        ComposeEnvKey.AGENT_STATUS_LINE:      build_status_line(inst_id),
        ComposeEnvKey.DOCKERIZED_CLAUDE_ROOT: DOCKERIZED_CLAUDE_ROOT,
        # Dynamic-key updates from optional_creds/
        **install_creds_flags(present_optional_cred_services()),   # INSTALL_<TOOL>=0|1 build flags
        **token_env_dict(optional_cred_tokens()),                  # per-service tokens (e.g. JIRA_API_TOKEN)
    })


def set_container_mounts(inst_id):
    """Stage per-launch bind-mounts via add_docker_mount. Sister to set_container_env
    (bind-mounts vs env vars); both run sequentially in setup_state. Two layers:
    the per-instance pair (workspace → /workspace, state dir → /home/claude/.claude)
    derived from inst_id, plus the always-on DOCKER_BASE_MOUNTS from paths.py
    (whose target strings already carry any `:ro` suffix)."""
    add_docker_mount(inst_id.workspace, "/workspace")
    add_docker_mount(inst_id.state_dir, CLAUDE_CONFIG_IN_CONTAINER)
    for source, target in DOCKER_BASE_MOUNTS.items():
        add_docker_mount(source, target)


def ensure_image(chain):
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
        ret = subprocess.call(["docker", "compose"] + compose_files + ["build"], env=_subprocess_env())
        if ret != 0:
            sys.exit(ret)
        prev_tag = target


def run_compose(chain, instance, claude_args, resume_flag, conf):
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
    container_env_args = [
        arg
        for k in CONTAINER_ENV_FORWARDS if k in _compose_env
        for arg in ("-e", f"{k}={_compose_env[k]}")
    ] + [
        arg
        for k, v in CONTAINER_ENV_FIXED.items()
        for arg in ("-e", f"{k}={v}")
    ]
    cmd = (
        ["docker", "compose"] + compose_args + ["run", "--rm", "-it", "--name", container_name]
        + [arg for src, tgt in _docker_mounts.items() for arg in ("-v", f"{src}:{tgt}")]
        + container_env_args      # per-key -e flags from CONTAINER_ENV_FORWARDS / CONTAINER_ENV_FIXED
        + conf_env_args(conf)     # -e flags setting each per-agent conf key=value in the container
        + ["claude-code"]
        + resume_flag             # present if a resumed session
        + claude_args             # leftover argv (unrecognised flags + unresolved positional) → claude
    )
    sys.exit(subprocess.call(cmd, env=_subprocess_env()))
