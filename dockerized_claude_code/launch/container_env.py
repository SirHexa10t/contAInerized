"""Container env staging + emission — the value store behind every
`--build-arg NAME=VALUE` and `-e NAME=VALUE` flag the launcher passes to
`docker build` / `docker run`.

Owns the launcher's set of env keys (the `ContainerEnvKey` enum) and the
buffer they're staged into. Other modules call `stage_container_env` (or
write to `_container_env` directly when batching) to declare values;
docker_config pulls them back out at flag-emission time:
  - build args — each layer's `tag.docker` lists the names its Dockerfile
    consumes (`[build] arg_forward`, `INSTALL_*` globs supported);
    docker_config.build_arg_flags resolves them against the buffer.
  - run env — `container_env_args` emits the always-on subset
    (CONTAINER_ENV_FORWARDS) as `-e` flags; tag-conditional names travel
    via `[run] env_forward` in the owning tag's `tag.docker`
    (docker_config.env_forward_flags), e.g. {firewall}'s
    WHITELIST_ADDRESSES.

Also owns the related formatters that shape user-side data into env dicts:
  - `install_creds_flags` — INSTALL_<TOOL>=0|1 build args from the
    present optional-cred services.
  - `token_env_dict` — per-service secret tokens forwarded as
    JIRA_API_TOKEN etc.
  - `conf_env_args` — flattens an engine conf dict into `-e KEY=VALUE`
    flags for the final `docker run`.

`set_container_env` is the per-launch orchestrator that bulk-stages
everything in one update — status line, cache-busters, cred-flag fan-out,
token forwards, and the in-container BASH_ENV literal.

tag_handlers / docker_config / run.py all import from here.
"""

import time
from collections.abc import Collection
from datetime import date
from enum import Enum, auto
from functools import cache
from typing import Any

from .claude_code_config import build_status_line
from .file_access import optional_cred_tokens, present_optional_cred_services
from .paths import (
    BASHRC_IN_CONTAINER, OPTIONAL_CREDS_MOUNTS, OPTIONAL_CREDS_TOKEN_ENV_VARS,
)
from .tags import Instance


# ============================================================
# Container env keys
# ============================================================
# Every static env-var name the launcher stages, collected here so the set is
# grepp-able and IDE-completable. Members are str-subclasses (via
# `class X(str, Enum)`), so each member transparently works as a dict key and
# as a `==` comparator against a string. The `__str__` override makes
# f-strings emit the value (`"DOCKER_GID"`), not the enum repr.
#
# Each member carries a `container_emit: bool` flag: True → emitted
# unconditionally as a `-e KEY=VALUE` run flag (via CONTAINER_ENV_FORWARDS);
# False → consumed as a build arg or a tag-conditional env_forward, both
# pulled by name from the buffer at emission time.
#
# Dynamic env-var names (per-service tokens from OPTIONAL_CREDS_TOKEN_ENV_VARS,
# INSTALL_<TOOL> build flags) are not enumerated here — they're derived at run
# time from paths-side configuration.

class ContainerEnvKey(str, Enum):
    # `auto()` resolves to the member's name via `_generate_next_value_` below,
    # so each entry just declares its `container_emit` flag — no duplicated
    # name string. The tuple `(auto(), <flag>)` keeps each member's value
    # unique so Python's enum metaclass doesn't alias same-flag members.

    @staticmethod
    def _generate_next_value_(name: str, start: int, count: int, last_values: list[Any]) -> str:
        # Called by the enum metaclass for each `auto()` in member declarations.
        # Returning the name makes the resolved tuple element equal to the
        # member's own name, so __new__ below can pass it to str.__new__ and
        # the resulting member's `.value` (and `str(member)`) is the name.
        # Must be declared BEFORE the members for the metaclass to pick it up.
        return name

    # Build args (pulled by each layer's `[build] arg_forward`)
    SOFTWARE_STACK_REFRESH   = (auto(), False)   # weekly cache-buster for curl-piped Dockerfile installs (uv, rich-cli, Claude Code, rustup, playwright); --refresh-installs overrides with a per-launch timestamp
    FORCE_INSTALLS_REFRESH   = (auto(), False)   # cache-buster for every INSTALL_<TOOL> RUN in the [code] Dockerfile; defaults to "stable" so cred-gated installs hit cache on normal launches; --refresh-installs sets a per-launch timestamp so failed/stale installs get retried
    DOCKER_GID               = (auto(), False)   # {dood} host docker group GID — `_dood` Dockerfile build-arg for /var/run/docker.sock access
    # Tag-conditional run env (pulled by the owning tag's `[run] env_forward`)
    WHITELIST_ADDRESSES      = (auto(), False)   # {firewall} pre-resolved `<ip>[:port]` / `<cidr>[:port]` tokens, space-separated — read by init-firewall.sh; forwarded only when {firewall} is active
    # Always-on run env (emitted as `-e KEY=VALUE` flags by container_env_args)
    AGENT_STATUS_LINE        = (auto(), True)    # pre-styled ANSI status line at the bottom of Claude Code
    BASH_ENV                 = (auto(), True)    # path to the bashrc that non-interactive bash sources at startup

    # Custom __new__ + __init__ so the str-mixin and the extra `container_emit`
    # attribute can coexist:
    #   __new__ — bridges str.__new__ (which only accepts one arg) by passing
    #             just the value; also pins _value_ so .value resolves correctly.
    #   __init__ — sets the per-member `container_emit` (visible to mypy as a
    #              regular instance attribute, unlike attrs set inside __new__).

    def __new__(cls, value: str, container_emit: bool) -> "ContainerEnvKey":
        obj = str.__new__(cls, value)
        obj._value_ = value
        return obj

    def __init__(self, value: str, container_emit: bool) -> None:
        self.container_emit = container_emit

    def __str__(self) -> str:                          # `f"{key}"` → "DOCKER_GID", not "ContainerEnvKey.DOCKER_GID"
        return self.name

    @classmethod
    @cache
    def container_emits(cls) -> tuple["ContainerEnvKey", ...]:
        """Members flagged `container_emit=True` — the subset that
        container_env_args emits as `-e KEY=VALUE` flags. Cached: enum
        membership is fixed at import time."""
        return tuple(m for m in cls if m.container_emit)


# ============================================================
# Always-on run-env emission list
# ============================================================
# CONTAINER_ENV_FORWARDS holds the keys container_env_args considers for
# unconditional `-e` emission. Two slices:
#   1. ContainerEnvKey.container_emits() — every enum member flagged as a
#      container `-e` emit. Values come from `_container_env` at run time
#      (staged by set_container_env).
#   2. OPTIONAL_CREDS_TOKEN_ENV_VARS values — the per-service token env-var
#      names (JIRA_API_TOKEN etc.). Values come from `_container_env` via
#      token_env_dict, which only stages tokens whose files exist on host.
#
# Keys whose values aren't staged at run time are silently skipped —
# that's how JIRA_API_TOKEN stays gated on optional_creds/jira/token, etc.
#
# TERM is intentionally *not* here — docker_config passes a bare `-e TERM`
# (value inherited from the launcher's terminal) on the run command itself.

CONTAINER_ENV_FORWARDS = (*ContainerEnvKey.container_emits(), *OPTIONAL_CREDS_TOKEN_ENV_VARS.values())


# ============================================================
# Container env accumulator
# ============================================================
# Populated by set_container_env + per-tag handlers; read at flag-emission
# time by docker_config (build_arg_flags / env_forward_flags) and
# container_env_args below. Keys are ContainerEnvKey members (which subclass
# str) plus dynamic INSTALL_<TOOL> / token-var str keys. Values are
# heterogeneous (int GID, str addresses, etc.) — `Any` since the dict is a
# serialization bag, not a typed schema.

_container_env: dict[str, Any] = {}


def stage_container_env(key: ContainerEnvKey, value: Any) -> None:
    """Buffer a single env entry (any value type — flag emission coerces to
    str at the docker boundary). Pass one of the ContainerEnvKey members as
    the key. set_container_env writes its bulk batch directly via
    `_container_env.update({...})` since it's in this module."""
    _container_env[key] = value


def staged_env() -> dict[str, str]:
    """Snapshot of the staged entries, values coerced to str — what
    docker_config resolves `arg_forward` / `env_forward` names against."""
    return {str(k): str(v) for k, v in _container_env.items()}


# ============================================================
# Env formatters for user-side contributions
# ============================================================
# These shape raw data the file_access layer discovered on disk into the
# {KEY: VALUE} dicts the env accumulator consumes. Bind-mount staging for the
# same user-side data goes through add_docker_mount in user_additions, not here.

def install_creds_flags(services: Collection[str]) -> dict[str, str]:
    """`{INSTALL_<TOOL>: '0' | '1'}` dict for the [code] Dockerfile's
    build-args. One entry per OPTIONAL_CREDS_MOUNTS service that has an
    associated CLI install (cli_name is not None — npmrc/pypirc are
    config-only, and the `home/` contents-mount entry isn't tied to any
    single tool). Value is '1' when the matching cred dir is present (in
    `services`), '0' otherwise. The Dockerfile branches on each flag to
    decide whether to install that CLI."""
    return {f"INSTALL_{name.upper()}": ("1" if name in services else "0")
            for name, (_, cli) in OPTIONAL_CREDS_MOUNTS.items()
            if cli is not None}


def token_env_dict(tokens: dict[str, str]) -> dict[str, str]:
    """`{<env_var>: <token_string>}` dict, translating `{service: token}`
    (from file_access.optional_cred_tokens) via OPTIONAL_CREDS_TOKEN_ENV_VARS.
    Each entry forwards a per-service token into the container as the env
    var the matching CLI expects."""
    return {OPTIONAL_CREDS_TOKEN_ENV_VARS[svc]: tok
            for svc, tok in tokens.items()
            if svc in OPTIONAL_CREDS_TOKEN_ENV_VARS}


def conf_env_args(conf: dict[str, str]) -> list[str]:
    """Convert an engine conf dict (from Instance.conf) into a list of
    `-e KEY=VALUE` args for `docker run`. Each conf entry becomes a runtime
    env var inside the container."""
    return [item for k, v in conf.items() for item in ("-e", f"{k}={v}")]


def container_env_args() -> list[str]:
    """The always-on `-e KEY=VALUE` arg list for the final `docker run`
    command — emits one pair per CONTAINER_ENV_FORWARDS key that actually
    has a value staged in `_container_env`. Mirrors the bind-mount
    flattening in docker_config.run_container, just for env vars."""
    return [
        arg
        for k in CONTAINER_ENV_FORWARDS if k in _container_env
        for arg in ("-e", f"{k}={_container_env[k]}")
    ]


# ============================================================
# Per-launch orchestration
# ============================================================

def set_container_env(inst: Instance, refresh_installs: bool = False) -> None:
    """Stage per-launch env vars in one bulk dict-update — called by run.py
    before docker build/run. Sister to docker_config's set_container_mounts
    (env vars vs bind-mounts); both run sequentially in setup_state.

    `refresh_installs` (driven by run.py's `--refresh-installs` CLI flag):
    when True, both refresh-cache-buster ARGs (SOFTWARE_STACK_REFRESH and
    FORCE_INSTALLS_REFRESH) get a fresh per-launch timestamp, forcing
    every install layer in the [code] Dockerfile to rebuild. Used to retry
    installs that failed in a prior launch (transient network issues,
    GitHub API rate limits, etc.) without manual `--no-cache` invocations.
    Default False — keeps SOFTWARE_STACK_REFRESH on its weekly rotation
    and FORCE_INSTALLS_REFRESH at "stable" so the cache hits."""
    refresh_value = f"forced-{int(time.time())}" if refresh_installs else None
    _container_env.update({
        ContainerEnvKey.SOFTWARE_STACK_REFRESH:  refresh_value or date.today().strftime("%Y-W%W"),
        ContainerEnvKey.FORCE_INSTALLS_REFRESH:  refresh_value or "stable",
        ContainerEnvKey.AGENT_STATUS_LINE:       build_status_line(inst),
        ContainerEnvKey.BASH_ENV:                BASHRC_IN_CONTAINER,
        # Dynamic-key updates from optional_creds/
        **install_creds_flags(present_optional_cred_services()),   # INSTALL_<TOOL>=0|1 build flags
        **token_env_dict(optional_cred_tokens()),                  # per-service tokens (e.g. JIRA_API_TOKEN)
    })
