"""Centralised path constants — every file and folder location the launcher
knows about. Host-side state files and dirs, project layout, container-side
bind-mount targets, defaults for workspace selection (including the host-shell
$AI_WORKSPACE override read at startup), and the bind-mount source paths the
docker compose YAMLs consume via ${...} substitutions.

Imports nothing from sibling launch/ modules — kept as the import root so it
can be pulled in anywhere without circular-import risk. Pure data + a couple
of host-environment reads (`$AI_WORKSPACE`, `os.getcwd()`) folded into the
DEFAULT_WORKSPACE expression."""

import os
from pathlib import Path


# ============================================================
# Project layout (paths inside the launcher repo)
# ============================================================
# DOCKERIZED_CLAUDE_ROOT anchors every host-side path the launcher knows about.
# It's the *only* place that uses `.parent` to locate the repo root — every
# other in-repo path (agents/, docker/, memory/, etc.) hangs off this constant,
# so a file relocation only needs the traversal count updated here. The compose
# YAMLs reach this via the ${DOCKERIZED_CLAUDE_ROOT} env-var substitution staged
# by docker_config.set_container_env.

DOCKERIZED_CLAUDE_ROOT = Path(__file__).resolve().parent.parent   # repo root — one above launch/
AGENTS_DIR = DOCKERIZED_CLAUDE_ROOT / "agents"                    # agent .md / .conf definitions
MEMORY_DIR = DOCKERIZED_CLAUDE_ROOT / "memory"                    # source-of-truth template files synced into per-instance MEMORY.md by agents_crud.sync_memory_templates
DEFAULT_CONF = AGENTS_DIR / "default.conf"
PROJECT_CUSTOM_SKILLS_DIR = DOCKERIZED_CLAUDE_ROOT / "custom_skills"   # project-bundled skills (paired with workspace .skills/)
SETTINGS_DIR = DOCKERIZED_CLAUDE_ROOT / "settings"                # container-mounted scripts + Claude Code settings (statusline, bashrc, etc.); DOCKER_BASE_MOUNTS inlines each leaf
TEMPLATES_DIR = DOCKERIZED_CLAUDE_ROOT / "launch" / "templates"   # source-side files that file_access plants on first launch (firewall whitelist preamble, optional_creds README)
OPTIONAL_CREDS_README_TEMPLATE = TEMPLATES_DIR / "optional_creds_readme.txt"   # planted as OPTIONAL_CREDS_DIR / OPTIONAL_CREDS_README_FILENAME on first launch
FIREWALL_WHITELIST_TEMPLATE    = TEMPLATES_DIR / "firewall_whitelist.txt"      # planted as FIREWALL_WHITELIST_FILE on first launch
DOCKER_DIR = DOCKERIZED_CLAUDE_ROOT / "docker"                    # Dockerfile + compose.yml + per-layer Dockerfile.<x> / compose.<x>.yml


# ============================================================
# Host-side persistent state — everything under ~/.claude-agents
# ============================================================

AGENTS_STATE = Path.home() / ".claude-agents"
ACCOUNT_FILE = AGENTS_STATE / ".claude.json"                       # shared OAuth account info
CREDENTIALS_FILE = AGENTS_STATE / ".credentials.json"             # shared API credentials
AGENT_WORKSPACE_MAP_FILE = AGENTS_STATE / "agent_workspace_map.json"
AGENT_MODES_MAP_FILE = AGENTS_STATE / "agent_modes_map.json"      # {instance_id: [mode, ...]}; only entries for instances with modes
CACHE_ROOT = AGENTS_STATE / "cache"

# {auto}-mode firewall whitelist — two files, separate purposes:
#   • DOMAINS_PENDING_RESOLVE_FILENAME — lives inside the per-instance state
#     dir (bind-mounted into the container at /home/claude/.claude/).
#     auto-addendum.md points the agent here to classify "I hit a connection
#     refused" → pending / failed / not-listed. Holds status + pending list +
#     failed list.
#   • RESOLVED_DOMAINS_CACHE_FILE — global cross-launch DNS cache at the
#     AGENTS_STATE root. Hosts already in this file (when it's fresh — the
#     TTL gate lives with the cache logic in network.py) short-circuit DNS
#     resolution: the launcher reuses the cached IPs directly. Rewritten at
#     end of every {auto} launch with the full resolved set, so successive
#     launches keep accumulating coverage while they stay within the TTL.
DOMAINS_PENDING_RESOLVE_FILENAME = "domains_pending_resolve.yml"
RESOLVED_DOMAINS_CACHE_FILE = AGENTS_STATE / "resolved_domains.txt"

# Everything under user_extras/ is for the user to populate with
# non-project-specific configuration: extra firewall whitelist entries,
# optional cred passthroughs for cloud CLIs, etc. Grouped under one dir so
# launcher-managed state at the AGENTS_STATE root (OAuth, workspace map,
# instance dirs) doesn't mix with hand-edited files.
USER_EXTRAS_DIR = AGENTS_STATE / "user_extras"
OPTIONAL_CREDS_DIR = USER_EXTRAS_DIR / "optional_creds"
FIREWALL_WHITELIST_FILE = USER_EXTRAS_DIR / "firewall_whitelist.txt"


# ============================================================
# Workspace selection
# ============================================================
# DEFAULTING_DIRS — directories that count as "neutral" launch points (typically
# $HOME, /tmp, etc.). Launching from one of these diverts workspace selection
# to a shared sandbox path (`/ai_workspace`) so the launcher never silently
# bind-mounts something like the user's whole home dir. Same list drives the
# picker's `(DEFAULT DIR)` tagging.

_HOME = Path.home()
DEFAULTING_DIRS = [
    str(_HOME),
    str(_HOME / "Desktop"),
    str(_HOME / "Downloads"),
    str(_HOME / "Pictures"),
    str(_HOME / "Videos"),
    str(_HOME / ".ssh"),
    "/tmp",
    "/var/tmp",
    "/",
]


# Workspace to suggest when prompting for a new instance. Primary choice is
# $AI_WORKSPACE (user's host-shell preference; same env-key the launcher writes
# to compose per launch but opposite direction) or — when launching from a
# DEFAULTING_DIR like $HOME — the /ai_workspace shared sandbox so the launcher
# never silently bind-mounts something like the user's whole home dir.
DEFAULT_WORKSPACE = (
    os.environ.get("AI_WORKSPACE")
    or ("/ai_workspace" if os.getcwd() in DEFAULTING_DIRS else os.getcwd())
)

# Safety net: fall back to $PWD when the primary choice doesn't resolve to a
# real directory (typo'd env, missing /ai_workspace sandbox, dangling symlink).
# `is_dir()` follows symlinks, so a symlink-to-dir passes through. menu_picker.
# ask_for_workspace re-validates the user's submitted path, so this is a UX
# nicety, not the correctness gate.
if not Path(DEFAULT_WORKSPACE).is_dir():
    DEFAULT_WORKSPACE = os.getcwd()


# ============================================================
# Container-side bind-mount targets + access modes
# ============================================================
# Where files appear *inside* the running container, plus the docker access-mode
# suffix appended to target strings with `:`. CLAUDE_HOME_IN_CONTAINER is the
# user's home directory in the container (mirrors `~` on the host conceptually);
# CLAUDE_CONFIG_IN_CONTAINER is its `.claude` subdir (Claude Code's per-user
# config root, where the agent state dir gets bind-mounted). Anything else that
# lands under /home/claude/... downstream (BASE mounts, optional creds, cache
# mounts) hangs off these. RO_MOUNT_OPTION is the only access mode this project
# uses; others (z/Z, cached/delegated, propagation) would join here.

CLAUDE_HOME_IN_CONTAINER = Path("/home/claude")
CLAUDE_CONFIG_IN_CONTAINER = CLAUDE_HOME_IN_CONTAINER / ".claude"
SKILLS_IN_CONTAINER = CLAUDE_CONFIG_IN_CONTAINER / "skills"
RO_MOUNT_OPTION = "ro"


# ============================================================
# {auto}-mode bind-mounts
# ============================================================
# Mounts staged when {auto} is in the chain — bind-mount the firewall init
# script + entrypoint wrapper into well-known paths inside the container.
# agent_composition._apply_auto iterates DOCKER_AUTO_MOUNTS via add_docker_mount;
# gated by the handler being called, not by a YAML overlay. Mirror of the
# DOCKER_BASE_MOUNTS pattern below, scoped to the {auto} chain step.
#
# Walrus bindings on the keys publish `INIT_FIREWALL_SH` and `AUTO_ENTRYPOINT_SH`
# as module attributes without a separate top-level statement, so the dict
# stays the single declaration site while the names remain importable for any
# future external use (currently none).

LOCAL_BIN_IN_CONTAINER = Path("/usr/local/bin")   # container target dir for the {auto} scripts

DOCKER_AUTO_MOUNTS = {
    (INIT_FIREWALL_SH   := DOCKER_DIR / "init-firewall.sh"):   f"{LOCAL_BIN_IN_CONTAINER}/init-firewall.sh:{RO_MOUNT_OPTION}",
    (AUTO_ENTRYPOINT_SH := DOCKER_DIR / "auto-entrypoint.sh"): f"{LOCAL_BIN_IN_CONTAINER}/auto-entrypoint.sh:{RO_MOUNT_OPTION}",
}


# ============================================================
# {DooD}-mode bind-mounts
# ============================================================
# Bind-mount the host's Docker socket into the container at the same path so
# the agent's `docker` CLI can drive the host's Docker daemon. Read-write —
# the socket needs both directions. The matching DOCKER_GID build-arg (staged
# by agent_composition._apply_dood) makes the in-image `docker` group match
# the host's so claude can read/write this socket. Iterated by _apply_dood.

DOCKER_DOOD_MOUNTS = {
    Path("/var/run/docker.sock"): "/var/run/docker.sock",
}


# ============================================================
# Always-on container bind-mounts
# ============================================================
# Source path (on host) → container target with any docker access-mode suffix
# (`:ro`) baked in. Iterated by docker_config.set_container_mounts, which
# calls add_docker_mount per entry — same {source: target} shape as the
# _docker_mounts accumulator the function feeds. These are the static mounts
# every launch gets — the equivalent of what compose.yml's `volumes:` block
# held before Python took over mount staging. Per-instance mounts (the picked
# workspace + the picked instance's state dir) stage inline next to this
# iteration since their host paths are derived from the picked instance, not
# constants.

DOCKER_BASE_MOUNTS = {
    # Per-instance state files (these source constants serve other modules too — audit, agents_crud)
    ACCOUNT_FILE:                               f"{CLAUDE_HOME_IN_CONTAINER}/.claude.json",                         # shared OAuth account info
    CREDENTIALS_FILE:                           f"{CLAUDE_CONFIG_IN_CONTAINER}/.credentials.json",                  # shared API credentials — Claude Code refreshes the token in place
    # Project-bundled sources — inlined since DOCKER_BASE_MOUNTS is their only consumer
    DOCKERIZED_CLAUDE_ROOT / "custom_commands": f"{CLAUDE_CONFIG_IN_CONTAINER}/commands:{RO_MOUNT_OPTION}",         # shared slash commands
    SETTINGS_DIR / "statusline.sh":             f"{CLAUDE_CONFIG_IN_CONTAINER}/statusline.sh:{RO_MOUNT_OPTION}",    # shared status-line script
    SETTINGS_DIR / "bashrc.sh":                 f"{CLAUDE_HOME_IN_CONTAINER}/.bashrc:{RO_MOUNT_OPTION}",            # sourced by every non-interactive bash via BASH_ENV
    SETTINGS_DIR / "_summary.py":               f"{CLAUDE_CONFIG_IN_CONTAINER}/_summary.py:{RO_MOUNT_OPTION}",      # backs summary_diff / summary_save_manifest in bashrc
    SETTINGS_DIR / "settings.json":             f"{CLAUDE_CONFIG_IN_CONTAINER}/settings.json:{RO_MOUNT_OPTION}",    # status-line wiring + other shared Claude Code settings
    SETTINGS_DIR / "keybindings.json":          f"{CLAUDE_CONFIG_IN_CONTAINER}/keybindings.json:{RO_MOUNT_OPTION}", # project-wide key bindings (Shift+Enter newline, etc.)
}


# ============================================================
# File extensions + filenames used by multiple modules
# ============================================================
# Well-defined leaf names + extensions + intra-state-dir relative paths that
# get composed against dynamic prefixes (state dirs, instance ids, etc.) at
# call sites. Centralised here so the file-path contract is in one place
# rather than scattered as magic strings.

# Agent-file extensions — pair with parse_stem-derived names: `<agent>{MD_EXT}`
# and `<agent or parent>{CONF_EXT}` in agents/.
MD_EXT = ".md"
CONF_EXT = ".conf"

# JSONL extension — Claude Code's per-turn input log + session-UUID transcripts.
JSONL_EXT = ".jsonl"

# Session JSONL written by Claude Code on each turn; distinct from the
# session-UUID JSONLs that hold the actual conversation. has_continuable_history
# filters this name out; last_used_mtime + audit use its presence as the signal.
HISTORY_JSONL_FILENAME = "history.jsonl"

# Per-instance copy of the agent's source .md, installed each launch.
INSTANCE_CLAUDE_MD_FILENAME = "CLAUDE.md"

# Relpath inside an instance's state dir holding Claude Code's per-project
# state (history.jsonl, session-UUID transcripts, MEMORY.md).
INSTANCE_PROJECTS_RELPATH = Path("projects")

# Relpath inside an instance's state dir holding the per-instance MEMORY.md.
INSTANCE_MEMORY_FILE_RELPATH = INSTANCE_PROJECTS_RELPATH / "-workspace" / "memory" / "MEMORY.md"

# Relpath inside an instance's state dir under which skill mount-points are
# pre-created (so Docker doesn't auto-create them as root).
INSTANCE_SKILLS_RELPATH = Path("skills")

# Filenames inside user-contributed dirs.
SKILL_MARKER_FILENAME = "SKILL.md"                   # required inside a skill dir for it to be recognised
WORKSPACE_SKILLS_DIRNAME = ".skills"                 # per-workspace skills folder name
OPTIONAL_CREDS_README_FILENAME = "README.txt"        # auto-created in optional_creds/ on first launch
OPTIONAL_CREDS_README_PATH     = OPTIONAL_CREDS_DIR / OPTIONAL_CREDS_README_FILENAME   # full host path the README is planted at
OPTIONAL_CREDS_TOKEN_FILENAME = "token"              # per-service plain-text token (e.g. jira/token → $JIRA_API_TOKEN)

# Memory-template filenames in MEMORY_DIR — synced into per-instance MEMORY.md
# at each launch by agents_crud.sync_memory_templates.
SEEK_SUMMARY_FILENAME = "seek_summary.md"            # always-active template
ADDENDUM_SUFFIX = "-addendum.md"                     # `<mode>{ADDENDUM_SUFFIX}` — mode-specific addendum filename (e.g. auto-addendum.md)

# Compose-file naming. The base file is `compose.yml`; per-layer files follow
# `compose.<step>.yml` (built by the `compose_layer_path` lambda at the bottom
# of this file).
COMPOSE_FILE_NAME = "compose.yml"
COMPOSE_FILE_PATH = DOCKER_DIR / COMPOSE_FILE_NAME   # full repo-relative path to the base compose.yml


# ============================================================
# Toolchain caches — shared across [prog] agents/sessions
# ============================================================
# Same relative path on host and in container; CACHE_MOUNTS pairs each host
# cache (CACHE_ROOT / rel) with its container destination (CLAUDE_HOME_IN_CONTAINER / rel).

CACHE_REL_PATHS = [
    # languages currently in the prog image
    ".cargo/registry",           # Rust crates (.crate tarballs + index)
    ".cargo/git",                # Rust git dependencies
    ".cache",                    # XDG cache: uv, pip, poetry, pre-commit, huggingface, torch, yarn-v1, go-build, ccache, ...
    # speculative — empty until the relevant language is added to the Dockerfile
    "go/pkg/mod",                # Go module cache
    ".npm",                      # npm (non-XDG by design)
    ".local/share/pnpm/store",   # pnpm content-addressed store
    ".m2/repository",            # Maven local repository
    ".gradle/caches",            # Gradle dependency + build caches
    ".gem",                      # Ruby gems
    ".cpanm",                    # Perl cpanminus work dir
    ".cpan",                     # Perl CPAN classic
    ".cabal/store",              # Haskell cabal package store
    ".stack/snapshots",          # Haskell stack resolver snapshots
]
CACHE_MOUNTS = {CACHE_ROOT / rel: CLAUDE_HOME_IN_CONTAINER / rel for rel in CACHE_REL_PATHS}


# ============================================================
# Optional credentials — host source → container target per recognised service
# ============================================================
# Each subpath under OPTIONAL_CREDS_DIR, when present on the host, gets
# bind-mounted to the matching location inside the container so the
# corresponding CLI (aws/gcloud/gh/etc.) just works. Read-write — cloud CLIs
# need to refresh tokens / write cache; presence on host is the opt-in.
# (The matching INSTALL_<TOOL> build-arg semantics — install_creds_flags
# in docker_config — are spread into the compose env in set_container_env.)

OPTIONAL_CREDS_MOUNTS = {
    "aws":     f"{CLAUDE_HOME_IN_CONTAINER}/.aws",
    "gcloud":  f"{CLAUDE_HOME_IN_CONTAINER}/.config/gcloud",
    "kube":    f"{CLAUDE_HOME_IN_CONTAINER}/.kube",
    "ssh":     f"{CLAUDE_HOME_IN_CONTAINER}/.ssh",
    "gh":      f"{CLAUDE_HOME_IN_CONTAINER}/.config/gh",
    "glab":    f"{CLAUDE_HOME_IN_CONTAINER}/.config/glab-cli",
    "jira":    f"{CLAUDE_HOME_IN_CONTAINER}/.config/.jira",
    "vercel":  f"{CLAUDE_HOME_IN_CONTAINER}/.local/share/com.vercel.cli",
    "railway": f"{CLAUDE_HOME_IN_CONTAINER}/.config/railway",
    "npmrc":   f"{CLAUDE_HOME_IN_CONTAINER}/.npmrc",
    "pypirc":  f"{CLAUDE_HOME_IN_CONTAINER}/.pypirc",
}

# Some services authenticate via an env-var token rather than (or alongside)
# a config file. For those, the launcher reads `optional_creds/<name>/token`
# on the host and forwards its contents as the corresponding env var inside
# the container. Key matches OPTIONAL_CREDS_MOUNTS above; value is the env
# var name the CLI looks for. Keep both maps in sync when adding a service.
OPTIONAL_CREDS_TOKEN_ENV_VARS = {
    "jira": "JIRA_API_TOKEN",
}


# ============================================================
# Path builders — every dynamic "var / constant" path lives here
# ============================================================
# When a filesystem path is determined at runtime (an instance's state dir, a
# typed service name, a chain step, etc.) the join is parameterised below as
# a lambda. Callers do `from .paths import <name>` and call the lambda; they
# never construct paths via `/` themselves. Fully-static composite paths
# (`OPTIONAL_CREDS_README_PATH`, `COMPOSE_FILE_PATH`, the DOCKER_*_MOUNTS
# values, etc.) live with their constituent constants further up the file
# rather than down here, since they don't need parameters. Net effect: every
# filesystem path the launcher touches is named in this file, just at the
# layer that earns its space.
#
# Naming convention: `_path` suffix on every builder (file or dir — the name
# describes WHAT, the type system handles HOW). Group comments call out what's
# being built.

# Per-state-dir files & subdirs (state_dir = ~/.claude-agents/<instance>/)
state_md_path           = lambda state_dir:       state_dir / INSTANCE_CLAUDE_MD_FILENAME
state_memory_path       = lambda state_dir:       state_dir / INSTANCE_MEMORY_FILE_RELPATH
state_projects_path     = lambda state_dir:       state_dir / INSTANCE_PROJECTS_RELPATH
state_skill_subdir_path = lambda state_dir, name: state_dir / INSTANCE_SKILLS_RELPATH / name
state_pending_yml_path  = lambda state_dir:       state_dir / DOMAINS_PENDING_RESOLVE_FILENAME

# Per-instance state directory itself (one level up from the per-state-dir files)
instance_state_dir_path = lambda instance:        AGENTS_STATE / instance

# Workspace-side skills (workspace is a runtime-provided host path)
workspace_skills_path   = lambda workspace:       Path(workspace) / WORKSPACE_SKILLS_DIRNAME
skill_marker_path       = lambda skill_dir:       skill_dir / SKILL_MARKER_FILENAME

# Optional credentials per service (service ∈ OPTIONAL_CREDS_MOUNTS keys)
optional_creds_service_path = lambda service:     OPTIONAL_CREDS_DIR / service
optional_creds_token_path   = lambda service:     OPTIONAL_CREDS_DIR / service / OPTIONAL_CREDS_TOKEN_FILENAME

# Memory + agent file lookups (filename / name supplied by caller)
memory_template_path    = lambda filename:        MEMORY_DIR / filename
agent_conf_path         = lambda name:            AGENTS_DIR / f"{name}{CONF_EXT}"

# Docker compose-layer YAML (step ∈ InstanceModifiers value strings, lowercased)
compose_layer_path      = lambda step:            DOCKER_DIR / f"compose.{step.lower()}.yml"
