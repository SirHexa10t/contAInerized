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

Container-side `-e KEY=VALUE` emission is declared on the enum itself: each
`ComposeEnvKey` member carries a `container_emit: bool` flag. The
`container_emits()` classmethod returns the flagged subset, which
`CONTAINER_ENV_FORWARDS` splices alongside the dynamic per-cred token
keys. `container_env_args` flattens the resulting set into the `-e ...`
arg list run_compose appends.

`set_container_env` is the per-launch orchestrator that bulk-stages
everything `docker compose build/run` needs in one update — agent name,
status line, build-context root, cred-flag fan-out, token forwards, and
the in-container BASH_ENV literal.

agent_modifiers_handler / docker_config / run.py all import from here.
"""

import os
from datetime import date
from enum import Enum, auto
from functools import cache
from typing import Any

from .claude_code_config import build_status_line
from .file_access import optional_cred_tokens, present_optional_cred_services
from .paths import (
    BASHRC_IN_CONTAINER, DOCKERIZED_CLAUDE_ROOT, OPTIONAL_CREDS_MOUNTS,
    OPTIONAL_CREDS_TOKEN_ENV_VARS,
)
from .structs import InstanceIdentity


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
# Each member carries a `container_emit: bool` flag declaring whether the
# launcher emits it as a `-e KEY=VALUE` flag at `docker compose run` time
# (True) or only uses it for compose-YAML `${...}` substitution at build time
# (False). `container_emits()` returns the flagged subset, which drives
# `CONTAINER_ENV_FORWARDS` below.
#
# Dynamic env-var names (per-service tokens from OPTIONAL_CREDS_TOKEN_ENV_VARS,
# build-arg INSTALL_<TOOL> flags) are not enumerated here — they're derived
# at run time from paths-side configuration.

class ComposeEnvKey(str, Enum):
    # `auto()` resolves to the member's name via `_generate_next_value_` below,
    # so each entry just declares its `container_emit` flag — no duplicated
    # name string. The tuple `(auto(), <flag>)` keeps each member's value
    # unique so Python's enum metaclass doesn't alias same-flag members.

    @staticmethod
    def _generate_next_value_(name, start, count, last_values):
        # Called by the enum metaclass for each `auto()` in member declarations.
        # Returning the name makes the resolved tuple element equal to the
        # member's own name, so __new__ below can pass it to str.__new__ and
        # the resulting member's `.value` (and `str(member)`) is the name.
        # Must be declared BEFORE the members for the metaclass to pick it up.
        return name

    # Image build chain — driven into compose-YAML ${...} substitution
    TARGET_IMAGE             = (auto(), False)   # compose.yml `image:` — current step's tag (set per chain step + once more in run_compose)
    PARENT_IMAGE             = (auto(), False)   # compose.<step>.yml `FROM ${PARENT_IMAGE}` — prior step's tag; not set on base
    SOFTWARE_STACK_REFRESH   = (auto(), False)   # weekly cache-buster for curl-piped Dockerfile installs (uv, rich-cli, Claude Code, rustup, playwright)
    # Per-instance identity
    AGENT_NAME               = (auto(), False)   # agent's clean name — substituted into compose.yml's `container_name:`
    # Mode-driven build-args (compose-YAML ${...} substitution into the per-mode Dockerfile)
    WHITELIST_ADDRESSES      = (auto(), False)   # {auto}-mode firewall list of pre-resolved `<ip>[:port]` / `<cidr>[:port]` tokens — read by init-firewall.sh inside the container via compose.auto.yml's environment: block
    DOCKER_GID               = (auto(), False)   # {DooD}-mode host docker group GID — Dockerfile.dood build-arg for /var/run/docker.sock access
    # Build/launch wiring (compose-YAML ${...} substitution)
    DOCKERIZED_CLAUDE_ROOT   = (auto(), False)   # repo root — `context: ${DOCKERIZED_CLAUDE_ROOT}` in every build block
    # Container-side env vars (emitted as `-e KEY=VALUE` flags by container_env_args)
    AGENT_STATUS_LINE        = (auto(), True)    # pre-styled ANSI status line at the bottom of Claude Code
    BASH_ENV                 = (auto(), True)    # path to the bashrc that non-interactive bash sources at startup

    # Custom __new__ + __init__ so the str-mixin and the extra `container_emit`
    # attribute can coexist:
    #   __new__ — bridges str.__new__ (which only accepts one arg) by passing
    #             just the value; also pins _value_ so .value resolves correctly.
    #   __init__ — sets the per-member `container_emit` (visible to mypy as a
    #              regular instance attribute, unlike attrs set inside __new__).

    def __new__(cls, value: str, container_emit: bool) -> "ComposeEnvKey":
        obj = str.__new__(cls, value)
        obj._value_ = value
        return obj

    def __init__(self, value: str, container_emit: bool) -> None:
        self.container_emit = container_emit

    def __str__(self) -> str:                          # `f"{key}"` → "TARGET_IMAGE", not "ComposeEnvKey.TARGET_IMAGE"
        return self.name

    @classmethod
    @cache
    def container_emits(cls) -> tuple["ComposeEnvKey", ...]:
        """Members flagged `container_emit=True` — the subset that
        container_env_args emits as `-e KEY=VALUE` flags. Cached: enum
        membership is fixed at import time."""
        return tuple(m for m in cls if m.container_emit)


# ============================================================
# Container env-var emission list
# ============================================================
# Container env vars are emitted as `-e KEY=VALUE` flags on the `docker
# compose run` command, rather than declared in compose.yml's
# `environment:` block. Mirror of the _docker_mounts pattern in docker_config:
# declarative wiring lives in Python, compose YAMLs reserved for the build
# graph + build-context root substitution.
#
# CONTAINER_ENV_FORWARDS holds the keys container_env_args considers for
# emission. Two slices:
#   1. ComposeEnvKey.container_emits() — every enum member flagged as a
#      container `-e` emit. Values come from `_compose_env` at run time
#      (staged by set_container_env for unconditional ones, or by a
#      per-mode handler for conditional ones — see _apply_web).
#   2. OPTIONAL_CREDS_TOKEN_ENV_VARS values — the per-service token env-var
#      names (JIRA_API_TOKEN etc.). Values come from `_compose_env` via
#      token_env_dict, which only stages tokens whose files exist on host.
#
# Keys whose values aren't staged at run_compose time are silently skipped —
# that's how JIRA_API_TOKEN stays gated on optional_creds/jira/token, etc.
#
# TERM is intentionally *not* here — it's purely shell-inherited (whatever
# the user's terminal sets), so compose.yml's `environment: [- TERM]`
# pass-through is the natural fit.

CONTAINER_ENV_FORWARDS = (*ComposeEnvKey.container_emits(), *OPTIONAL_CREDS_TOKEN_ENV_VARS.values())


# ============================================================
# Compose env accumulator
# ============================================================
# Populated by set_container_env + per-mode handlers + ensure_image + run_compose;
# read at subprocess invocation time. Keys are ComposeEnvKey members (which
# subclass str) plus dynamic INSTALL_<TOOL> / token-var str keys. Values are
# heterogeneous (image-tag strs, DOCKER_GID int, etc.) — `Any` since the dict
# is a serialization bag, not a typed schema.

_compose_env: dict[str, Any] = {}


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
    """`{INSTALL_<TOOL>: '0' | '1'}` dict for Dockerfile.code's build-args.
    One entry per OPTIONAL_CREDS_MOUNTS service; value is '1' when the
    matching cred dir is present (in `services`), '0' otherwise.
    Dockerfile.code branches on each flag to decide whether to install
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
    command — emits one pair per CONTAINER_ENV_FORWARDS key that actually
    has a value staged in `_compose_env`. Mirrors the bind-mount flattening
    in docker_config.run_compose, just for env vars."""
    return [
        arg
        for k in CONTAINER_ENV_FORWARDS if k in _compose_env
        for arg in ("-e", f"{k}={_compose_env[k]}")
    ]


# ============================================================
# Per-launch orchestration
# ============================================================

def set_container_env(inst_id: InstanceIdentity) -> None:
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
        ComposeEnvKey.BASH_ENV:               BASHRC_IN_CONTAINER,
        ComposeEnvKey.DOCKERIZED_CLAUDE_ROOT: DOCKERIZED_CLAUDE_ROOT,
        # Dynamic-key updates from optional_creds/
        **install_creds_flags(present_optional_cred_services()),   # INSTALL_<TOOL>=0|1 build flags
        **token_env_dict(optional_cred_tokens()),                  # per-service tokens (e.g. JIRA_API_TOKEN)
    })
