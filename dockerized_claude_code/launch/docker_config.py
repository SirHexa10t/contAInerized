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

Compose env-var keys are module-level UPPERCASE string constants (TARGET_IMAGE,
AGENT_NAME, …). External callers import the constant by name and pass it to
stage_compose_env, so a typo surfaces as ImportError rather than a silently-
missing $VAR.

Imports from paths (filesystem constants, with the few still-used compose ${...}
sources renamed on import to avoid colliding with same-named env-key constants)
and file_access (the optional-creds primitives, plus read_json_field for the
OAuth-email lookup the status line uses). agent_composition imports
stage_compose_env / add_docker_mount (plus WHITELIST_ADDRESSES / DOCKER_GID) from
here; run.py is the top-level consumer.
"""

import os
import shutil
import subprocess
import sys
from datetime import date

from .file_access import optional_cred_tokens, present_optional_cred_services, read_json_field
from .network import is_critical_pending, start_firewall_updater, wait_for_critical_addresses
from .paths import (
    # Paths used by name as compose env-var values — aliased on import to free
    # the bare name for the same-named env-key constant below. Only the auto
    # mode's scripts + the build-context root still travel this route; the
    # rest of the always-on mount sources reach docker via Python (DOCKER_BASE_MOUNTS
    # + set_container_mounts), so no aliasing is needed for them.
    AUTO_ENTRYPOINT_SH as _AUTO_ENTRYPOINT_SH_PATH,
    DOCKERIZED_CLAUDE_ROOT as _DOCKERIZED_CLAUDE_ROOT_PATH,
    INIT_FIREWALL_SH as _INIT_FIREWALL_SH_PATH,
    # Paths used directly — no collision with an env-key constant.
    ACCOUNT_FILE, CLAUDE_CONFIG_IN_CONTAINER, COMPOSE_FILE_NAME, DOCKER_BASE_MOUNTS,
    DOCKER_DIR, OPTIONAL_CREDS_MOUNTS, OPTIONAL_CREDS_TOKEN_ENV_VARS,
)


# ============================================================
# Compose env-var keys
# ============================================================

# --- Image build chain ---
TARGET_IMAGE           = "TARGET_IMAGE"            # compose.yml `image:` — current step's tag (set per chain step + once more in run_compose)
PARENT_IMAGE           = "PARENT_IMAGE"            # compose.<step>.yml `FROM ${PARENT_IMAGE}` — prior step's tag; not set on base
SOFTWARE_STACK_REFRESH = "SOFTWARE_STACK_REFRESH"  # weekly cache-buster for curl-piped Dockerfile installs (uv, rich-cli, Claude Code, rustup)

# --- Per-instance identity / forwarded env ---
AGENT_NAME             = "AGENT_NAME"              # agent's clean name — substituted into compose.yml's `container_name:`
AGENT_STATUS_LINE      = "AGENT_STATUS_LINE"       # pre-styled ANSI status line at the bottom of Claude Code (forwarded into container)

# --- Mode-driven keys ---
WHITELIST_ADDRESSES    = "WHITELIST_ADDRESSES"      # {auto}-mode firewall list of pre-resolved `<ip>[:port]` / `<cidr>[:port]` tokens (space-joined) — read by init-firewall.sh inside the container
DOCKER_GID             = "DOCKER_GID"              # host docker group GID — Dockerfile.dood build-arg for /var/run/docker.sock access

# --- Build/launch wiring (still referenced by ${...} in compose YAMLs) ---
# Most always-on mount sources now reach docker via Python (DOCKER_BASE_MOUNTS
# + add_docker_mount); only the auto-mode bind-mount sources and the build-context
# root still travel as compose env-var substitutions, so this block is short.
DOCKERIZED_CLAUDE_ROOT = "DOCKERIZED_CLAUDE_ROOT"   # repo root — `context: ${DOCKERIZED_CLAUDE_ROOT}` in every build block
INIT_FIREWALL_SH       = "INIT_FIREWALL_SH"         # docker/init-firewall.sh — {auto}-mode iptables script
AUTO_ENTRYPOINT_SH     = "AUTO_ENTRYPOINT_SH"       # docker/auto-entrypoint.sh — {auto}-mode container entrypoint wrapper


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
    args = ["-f", str(DOCKER_DIR / COMPOSE_FILE_NAME)]
    for step in chain[1:]:
        args += ["-f", str(DOCKER_DIR / f"compose.{step.lower()}.yml")]
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

def _build_status_line(inst_id):
    """ANSI label for Claude Code's bottom status line — cyan agent + grey
    workspace + green email + blue instance (`<agent>__<session>`). The
    `<email> :` prefix drops out when .claude.json is missing or lacks a
    recognisable email field. Accepts any InstanceIdentity (or subclass —
    SessionIdentity works too); only reads .agent / .session / .workspace /
    .instance."""
    CYAN, BLUE, GREEN, GREY, RESET = "\033[36m", "\033[34m", "\033[32m", "\033[90m", "\033[0m"
    session_complete = (f"{inst_id.agent.replace('-', ' ').title()} - {inst_id.session.replace('-', ' ').replace('_', ' ').title()}"
                        f" {GREY}( {inst_id.workspace} ){RESET}")
    mail_at_instance = f"{BLUE}{inst_id.instance}{RESET}"
    email = read_json_field(ACCOUNT_FILE, "oauthAccount", "emailAddress")
    if email:
        mail_at_instance = f"{GREEN}{email}{RESET} : {mail_at_instance}"
    return f"{CYAN}● {session_complete}\t\t{mail_at_instance}"


def set_container_env(inst_id):
    """Stage per-launch compose env vars in one bulk dict-update — called by run.py
    before docker compose build/run. Sister to set_container_mounts (env vars vs
    bind-mounts); both run sequentially in setup_state. Accepts any
    InstanceIdentity (or subclass); only reads .agent for the container name,
    plus what the status-line builder consumes."""
    _compose_env.update({
        SOFTWARE_STACK_REFRESH: date.today().strftime("%Y-W%W"),
        AGENT_NAME:             inst_id.agent,
        AGENT_STATUS_LINE:      _build_status_line(inst_id),
        DOCKERIZED_CLAUDE_ROOT: _DOCKERIZED_CLAUDE_ROOT_PATH,
        INIT_FIREWALL_SH:       _INIT_FIREWALL_SH_PATH,
        AUTO_ENTRYPOINT_SH:     _AUTO_ENTRYPOINT_SH_PATH,
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
        stage_compose_env(TARGET_IMAGE, target)
        if prev_tag:
            stage_compose_env(PARENT_IMAGE, prev_tag)
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
    stage_compose_env(TARGET_IMAGE, chain_image_tag(chain))
    compose_args = chain_compose_files(chain)
    print(f"\033]0;Claude Code — {instance}\007", end="", flush=True)
    # Phase 1 await: block for critical Anthropic addresses, stage them as the
    # initial WHITELIST_ADDRESSES. Phase 2 (rest of the whitelist) is still
    # resolving in the background; the updater thread (spawned below) handles
    # it via `docker exec` once the container is up.
    if is_critical_pending():
        print("  Waiting for critical {auto}-mode firewall addresses...", flush=True)
    if (addresses := wait_for_critical_addresses()) is not None:
        stage_compose_env(WHITELIST_ADDRESSES, " ".join(addresses))
    container_name = f"claude-code_{instance}"
    # Spawn the updater BEFORE subprocess.call (which blocks for the container's
    # lifetime) — the daemon thread will see the container come up shortly and
    # start draining Phase 2 results onto iptables. No-op for non-{auto} launches.
    start_firewall_updater(container_name)
    cmd = (
        ["docker", "compose"] + compose_args + ["run", "--rm", "-it", "--name", container_name]
        + [arg for src, tgt in _docker_mounts.items() for arg in ("-v", f"{src}:{tgt}")]
        + conf_env_args(conf)     # -e flags setting each per-agent conf key=value in the container
        + ["claude-code"]
        + resume_flag             # present if a resumed session
        + claude_args             # leftover argv (unrecognised flags + unresolved positional) → claude
    )
    sys.exit(subprocess.call(cmd, env=_subprocess_env()))
