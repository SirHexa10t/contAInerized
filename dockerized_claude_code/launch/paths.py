"""Centralised path constants — every file and folder location the launcher
knows about. Host-side state files and dirs, project layout, container-side
bind-mount targets, defaults for workspace selection.

Imports nothing from sibling launch/ modules — kept as the import root so it
can be pulled in anywhere without circular-import risk. Pure data only;
domain logic (parsing the agent filename grammar, picking modes, etc.) lives
in the modules that consume these paths."""

from pathlib import Path


# === Project layout (paths inside the launcher repo) ===

PROJECT = Path(__file__).resolve().parent.parent   # repo root — one above launch/
AGENTS_DIR = PROJECT / "agents"
MEMORY_DIR = PROJECT / "memory"                    # source-of-truth template files synced into per-instance MEMORY.md by agents_crud.sync_memory_templates
DEFAULT_CONF = AGENTS_DIR / "default.conf"
PROJECT_CUSTOM_SKILLS_DIR = PROJECT / "custom_skills"


# === Host-side persistent state — everything under ~/.claude-agents ===

AGENTS_STATE = Path.home() / ".claude-agents"
ACCOUNT_FILE = AGENTS_STATE / ".claude.json"                       # shared OAuth account info
CREDENTIALS_FILE = AGENTS_STATE / ".credentials.json"             # shared API credentials
AGENT_WORKSPACE_MAP_FILE = AGENTS_STATE / "agent_workspace_map.json"
AGENT_MODES_MAP_FILE = AGENTS_STATE / "agent_modes_map.json"      # {instance_id: [mode, ...]}; only entries for instances with modes
CACHE_ROOT = AGENTS_STATE / "cache"
OPTIONAL_CREDS_DIR = AGENTS_STATE / "optional_creds"
FIREWALL_WHITELIST_FILE = AGENTS_STATE / "firewall_whitelist.txt"


# === Workspace selection ===
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


# === Container-side bind-mount targets ===

SKILLS_IN_CONTAINER = "/home/claude/.claude/skills"
CACHE_HOME_IN_CONTAINER = Path("/home/claude")


# === Toolchain caches — shared across [prog] agents/sessions ===
# Same relative path on host and in container; CACHE_MOUNTS pairs each host
# cache (CACHE_ROOT / rel) with its container destination (CACHE_HOME_IN_CONTAINER / rel).

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
CACHE_MOUNTS = {CACHE_ROOT / rel: CACHE_HOME_IN_CONTAINER / rel for rel in CACHE_REL_PATHS}


# === Optional credentials — host source → container target per recognised service ===
# Each subpath under OPTIONAL_CREDS_DIR, when present on the host, gets
# bind-mounted to the matching location inside the container so the
# corresponding CLI (aws/gcloud/gh/etc.) just works. Read-write — cloud CLIs
# need to refresh tokens / write cache; presence on host is the opt-in.
# (The matching INSTALL_<TOOL> build-arg semantics live with
# optional_creds_install_env in user_additions.)

OPTIONAL_CREDS_MOUNTS = {
    "aws":     "/home/claude/.aws",
    "gcloud":  "/home/claude/.config/gcloud",
    "kube":    "/home/claude/.kube",
    "ssh":     "/home/claude/.ssh",
    "gh":      "/home/claude/.config/gh",
    "glab":    "/home/claude/.config/glab-cli",
    "jira":    "/home/claude/.config/.jira",
    "vercel":  "/home/claude/.local/share/com.vercel.cli",
    "railway": "/home/claude/.config/railway",
    "npmrc":   "/home/claude/.npmrc",
    "pypirc":  "/home/claude/.pypirc",
}

# Some services authenticate via an env-var token rather than (or alongside)
# a config file. For those, the launcher reads `optional_creds/<name>/token`
# on the host and forwards its contents as the corresponding env var inside
# the container. Key matches OPTIONAL_CREDS_MOUNTS above; value is the env
# var name the CLI looks for. Keep both maps in sync when adding a service.
OPTIONAL_CREDS_TOKEN_ENV_VARS = {
    "jira": "JIRA_API_TOKEN",
}
