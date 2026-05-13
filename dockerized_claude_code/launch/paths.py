"""Centralised path constants — every file and folder location the launcher
knows about. Host-side state files and dirs, project layout, container-side
bind-mount targets, defaults for workspace selection (including the host-shell
$AI_WORKSPACE override read at startup), and the bind-mount source paths the
docker compose YAMLs consume via ${...} substitutions.

Imports nothing from sibling launch/ modules — kept as the import root so it
can be pulled in anywhere without circular-import risk. Pure data + one small
env-var reader (read_workspace_pref) that fits the workspace-selection group."""

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
# FALLBACK_WORKSPACE — shared sandbox path used when launching from a "neutral"
# directory (one in DEFAULTING_DIRS — typically $HOME), so the launcher never
# silently bind-mounts something like the user's whole home dir.
# DEFAULTING_DIRS — directories that divert workspace selection to
# FALLBACK_WORKSPACE; same list also drives the picker's `(DEFAULT DIR)` tagging.

FALLBACK_WORKSPACE = "/ai_workspace"
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


def read_workspace_pref():
    """The user's optional shell-side $AI_WORKSPACE default — read at startup
    to seed DEFAULT_WORKSPACE in agents_crud. Same env-key name as the compose
    substitution the launcher writes per launch, but distinct in direction:
    this reads what the user set in their shell, whereas docker_config writes
    the same key into the compose-env accumulator per launch."""
    return os.environ.get("AI_WORKSPACE")


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
# {auto}-mode bind-mount sources
# ============================================================
# Host-side script paths referenced by compose.auto.yml as ${...} substitutions
# — these can't move into DOCKER_BASE_MOUNTS because their mounts must remain
# conditional via the compose layer (compose.auto.yml only loads when {auto} is
# active). Matching env-key constants live in docker_config so the launcher
# feeds the substitution at run time; without this indirection the YAML would
# carry a fragile `../init-firewall.sh` traversal.

INIT_FIREWALL_SH = DOCKER_DIR / "init-firewall.sh"
AUTO_ENTRYPOINT_SH = DOCKER_DIR / "auto-entrypoint.sh"


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
INSTANCE_MEMORY_RELPATH = INSTANCE_PROJECTS_RELPATH / "-workspace" / "memory"
INSTANCE_MEMORY_FILE_RELPATH = INSTANCE_MEMORY_RELPATH / "MEMORY.md"

# Relpath inside an instance's state dir under which skill mount-points are
# pre-created (so Docker doesn't auto-create them as root).
INSTANCE_SKILLS_RELPATH = Path("skills")

# Filenames inside user-contributed dirs.
SKILL_MARKER_FILENAME = "SKILL.md"                   # required inside a skill dir for it to be recognised
WORKSPACE_SKILLS_DIRNAME = ".skills"                 # per-workspace skills folder name
OPTIONAL_CREDS_README_FILENAME = "README.txt"        # auto-created in optional_creds/ on first launch
OPTIONAL_CREDS_TOKEN_FILENAME = "token"              # per-service plain-text token (e.g. jira/token → $JIRA_API_TOKEN)

# Memory-template filenames in MEMORY_DIR — synced into per-instance MEMORY.md
# at each launch by agents_crud.sync_memory_templates.
SEEK_SUMMARY_FILENAME = "seek_summary.md"            # always-active template
ADDENDUM_SUFFIX = "-addendum.md"                     # `<mode>{ADDENDUM_SUFFIX}` — mode-specific addendum filename (e.g. auto-addendum.md)

# Compose-file naming. The base file is always included; per-layer files use
# the `compose.<step>.yml` pattern — written inline at the one call site in
# docker_config.chain_compose_files since centralising the f-string adds more
# fragmentation than it saves.
COMPOSE_FILE_NAME = "compose.yml"


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
# (The matching INSTALL_<TOOL> build-arg semantics live with
# optional_creds_install_env in user_additions.)

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
