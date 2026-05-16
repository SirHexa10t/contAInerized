"""Compose-side env-var staging + emission.

Owns the launcher's set of compose env-var keys (the `ComposeEnvKey` enum)
and the buffer that docker-compose subprocess calls read at substitution
time. Other modules call `stage_compose_env` (or write to `_compose_env`
directly when batching) to declare what they want; `subprocess_env()`
overlays the buffer onto `os.environ` for each subprocess invocation.

Also owns the related formatters that shape user-side data into compose
env-dicts:
  - `install_creds_flags` — INSTALL_<TOOL>=0|1 build args from the
    present optional-cred services.
  - `token_env_dict` — per-service secret tokens forwarded as
    JIRA_API_TOKEN etc.
  - `conf_env_args` — flattens an agent .conf dict into `-e KEY=VALUE`
    flags for the final `docker compose run`.

The container-side emission lists (`CONTAINER_ENV_FORWARDS` /
`CONTAINER_ENV_FIXED`) live here too — declarative wiring of which
compose entries flow into the container as `-e` flags. `container_env_args`
flattens them into the actual `-e ...` arg list run_compose appends.

`set_container_env` is the per-launch orchestrator that bulk-stages
everything `docker compose build/run` needs in one update — agent name,
status line, build-context root, cred-flag fan-out, and token forwards.

agent_composition / docker_config / run.py all import from here.
"""

import os
from datetime import date
from enum import Enum

from .claude_code_config import build_status_line
from .file_access import optional_cred_tokens, present_optional_cred_services
from .paths import (
    CLAUDE_HOME_IN_CONTAINER, DOCKERIZED_CLAUDE_ROOT, OPTIONAL_CREDS_MOUNTS,
    OPTIONAL_CREDS_TOKEN_ENV_VARS,
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
# — that's important for the `-e KEY=VALUE` flag emission in container_env_args.
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
    # Container-side env vars (emitted as `-e KEY=VALUE` flags)
    AGENT_STATUS_LINE      = "AGENT_STATUS_LINE"       # pre-styled ANSI status line at the bottom of Claude Code
    BASH_ENV               = "BASH_ENV"                # path to the bashrc that non-interactive bash sources at startup

    def __str__(self):                                 # `f"{key}"` → "TARGET_IMAGE", not "ComposeEnvKey.TARGET_IMAGE"
        return self.value


# ============================================================
# Container env-var emission lists
# ============================================================
# Container env vars are emitted as `-e KEY=VALUE` flags on the `docker
# compose run` command, rather than declared in compose.yml's
# `environment:` block. Mirror of the _docker_mounts pattern in docker_config:
# declarative wiring lives in Python, compose YAMLs reserved for the build
# graph + build-context root substitution.
#
# CONTAINER_ENV_FORWARDS holds keys whose values are already in `_compose_env`
# (staged by stage_compose_env elsewhere — AGENT_STATUS_LINE from
# set_container_env, and the optional-creds token vars from token_env_dict).
# Keys absent from `_compose_env` at run_compose time are silently skipped —
# that's how the JIRA_API_TOKEN passthrough stays conditional on
# optional_creds/jira/token being present.
#
# CONTAINER_ENV_FIXED holds key→value pairs with constant in-container values
# (BASH_ENV). Emitted unconditionally.
#
# TERM is intentionally *not* here — it's purely shell-inherited (whatever
# the user's terminal sets), so compose.yml's `environment: [- TERM]`
# pass-through is the natural fit.

CONTAINER_ENV_FORWARDS = (ComposeEnvKey.AGENT_STATUS_LINE, *OPTIONAL_CREDS_TOKEN_ENV_VARS.values())
CONTAINER_ENV_FIXED    = {ComposeEnvKey.BASH_ENV: f"{CLAUDE_HOME_IN_CONTAINER}/.bashrc"}


# ============================================================
# Compose env accumulator
# ============================================================

_compose_env: dict[str, object] = {}    # populated by set_container_env + per-mode handlers + ensure_image + run_compose; read at subprocess invocation time. Keys are ComposeEnvKey members (which subclass str) plus dynamic INSTALL_<TOOL> / token-var str keys.


def stage_compose_env(key: ComposeEnvKey, value) -> None:
    """Buffer a single compose env-var entry (any value type — `subprocess_env`
    coerces to str at the subprocess boundary). Pass one of the
    ComposeEnvKey members as the key. set_container_env writes its bulk
    batch directly via `_compose_env.update({...})` since it's in this
    module."""
    _compose_env[key] = value


def subprocess_env() -> dict[str, str]:
    """Host env overlaid with the staged compose entries (values coerced to
    str at this boundary so the accumulator can hold Path/int/etc.) —
    passed as `env=` to every docker-compose subprocess."""
    return {**os.environ, **{k: str(v) for k, v in _compose_env.items()}}


# ============================================================
# Compose-env formatters for user-side contributions
# ============================================================
# These shape raw data the file_access layer discovered on disk into the
# {KEY: VALUE} dicts the compose env accumulator consumes. Bind-mount staging
# for the same user-side data goes through add_docker_mount in
# user_additions, not here.

def install_creds_flags(services) -> dict[str, str]:
    """`{INSTALL_<TOOL>: '0' | '1'}` dict for Dockerfile.prog's build-args.
    One entry per OPTIONAL_CREDS_MOUNTS service; value is '1' when the
    matching cred dir is present (in `services`), '0' otherwise.
    Dockerfile.prog branches on each flag to decide whether to install
    that CLI."""
    return {f"INSTALL_{name.upper()}": ("1" if name in services else "0")
            for name in OPTIONAL_CREDS_MOUNTS}


def token_env_dict(tokens: dict[str, str]) -> dict[str, str]:
    """`{<env_var>: <token_string>}` dict, translating `{service: token}`
    (from file_access.optional_cred_tokens) via OPTIONAL_CREDS_TOKEN_ENV_VARS.
    Each entry forwards a per-service token into the container as the env
    var the matching CLI expects."""
    return {OPTIONAL_CREDS_TOKEN_ENV_VARS[svc]: tok
            for svc, tok in tokens.items()
            if svc in OPTIONAL_CREDS_TOKEN_ENV_VARS}


def conf_env_args(conf: dict[str, str]) -> list[str]:
    """Convert a per-agent `.conf` dict (from file_access.load_conf) into
    a list of `-e KEY=VALUE` args for `docker compose run`. Each conf entry
    becomes a runtime env var inside the container."""
    return [item for k, v in conf.items() for item in ("-e", f"{k}={v}")]


def container_env_args() -> list[str]:
    """The `-e KEY=VALUE` arg list for the final `docker compose run`
    command — forwards each ComposeEnvKey from CONTAINER_ENV_FORWARDS that
    actually has a value staged, plus the always-on CONTAINER_ENV_FIXED
    entries. Mirrors the bind-mount flattening in docker_config.run_compose,
    just for env vars."""
    return [
        arg
        for k in CONTAINER_ENV_FORWARDS if k in _compose_env
        for arg in ("-e", f"{k}={_compose_env[k]}")
    ] + [
        arg
        for k, v in CONTAINER_ENV_FIXED.items()
        for arg in ("-e", f"{k}={v}")
    ]


# ============================================================
# Per-launch orchestration
# ============================================================

def set_container_env(inst_id) -> None:
    """Stage per-launch compose env vars in one bulk dict-update — called by
    run.py before docker compose build/run. Sister to docker_config's
    set_container_mounts (env vars vs bind-mounts); both run sequentially
    in setup_state. Accepts any InstanceIdentity (or subclass); reads
    .agent for the container name, plus whatever the status-line builder
    consumes."""
    _compose_env.update({
        ComposeEnvKey.SOFTWARE_STACK_REFRESH: date.today().strftime("%Y-W%W"),
        ComposeEnvKey.AGENT_NAME:             inst_id.agent,
        ComposeEnvKey.AGENT_STATUS_LINE:      build_status_line(inst_id),
        ComposeEnvKey.DOCKERIZED_CLAUDE_ROOT: DOCKERIZED_CLAUDE_ROOT,
        # Dynamic-key updates from optional_creds/
        **install_creds_flags(present_optional_cred_services()),   # INSTALL_<TOOL>=0|1 build flags
        **token_env_dict(optional_cred_tokens()),                  # per-service tokens (e.g. JIRA_API_TOKEN)
    })
