"""Centralised path constants — every file and folder location the launcher
knows about. Host-side state files and dirs, project layout, container-side
bind-mount targets, defaults for workspace selection (including the host-shell
$AI_WORKSPACE override read at startup), and the bind-mount source paths the
docker build/run flags consume at emission time.

True leaf: imports nothing in-project. Pure constants + path-builder lambdas,
plus a couple of host-environment reads (`$AI_WORKSPACE`, `os.getcwd()`)
folded into the DEFAULT_WORKSPACE expression. Directory *contents* lookups
(the agents/ name → md-path index) live in file_access.agent_md_index — disk
listing is file-access work, not a path constant."""

import os
from pathlib import Path
from typing import Callable, Iterator


# ============================================================
# Project layout (paths inside the launcher repo)
# ============================================================
# DOCKERIZED_CLAUDE_ROOT anchors every host-side path the launcher knows about.
# It's the *only* place that uses `.parent` to locate the repo root — every
# other in-repo path (agents/, docker/, memory/, etc.) hangs off this constant,
# so a file relocation only needs the traversal count updated here. The docker
# YAMLs reach this via the ${DOCKERIZED_CLAUDE_ROOT} env-var substitution staged
# by docker_config.set_container_env.

DOCKERIZED_CLAUDE_ROOT = Path(__file__).resolve().parent.parent   # repo root — one above launch/
AGENTS_DIR = DOCKERIZED_CLAUDE_ROOT / "agents"                    # agent .md / .lego + kind subtrees (engine/ profession/ specialty/ policy/)
ENGINE_DIR = AGENTS_DIR / "engine"                                # engine tags — engine/<name>/{tag.info, engine.conf}
DEFAULT_CONF = ENGINE_DIR / "default" / "engine.conf"             # fallback engine conf when an agent names none
SETTINGS_DIR = DOCKERIZED_CLAUDE_ROOT / "settings"                # container-mounted scripts + Claude Code settings (statusline, bashrc, etc.); DOCKER_BASE_MOUNTS inlines each leaf
BASE_SETTINGS_FILE = SETTINGS_DIR / "settings.json"                # shared Claude Code settings base — merged with each instance's policy fragments into <state>/settings.json (agents_crud.install_settings); NOT mounted directly
SHARED_COMMANDS_DIR = DOCKERIZED_CLAUDE_ROOT / "custom_commands"   # slash commands EVERY instance gets; assembled into state_commands_dir per launch
COMMANDS_DIR_NAME = "_commands"                                   # dirname under agents/ holding every TAG-granted slash command — split out because the registry validates against a parameterised tree root (scan_all(agents_dir)), so it needs the name, not the composed repo path. Underscore-prefixed to read as the project's established "internal asset, not a tag" marker (_muxer, _quickie) beside the four kind subtrees — and NOT "[commands]", which would wear a profession's punctuation while being no tag, and is a shell glob-class that mangles hand-typed paths
AGENTS_COMMANDS_DIR = AGENTS_DIR / COMMANDS_DIR_NAME              # one file per command; a tag grants one by NAME (`commands = [...]` in its tag.info), so several tags can share a file and every specialized command is findable in one place. Sits safely beside the four kind subtrees: the scanners walk only engine/profession/specialty/policy
TEMPLATE_FILES_DIR = DOCKERIZED_CLAUDE_ROOT / "launch" / "template_files"   # source-side files that file_access plants on first launch (firewall whitelist preamble, optional_creds README)
OPTIONAL_CREDS_README_TEMPLATE = TEMPLATE_FILES_DIR / "optional_creds_readme.txt"   # planted as OPTIONAL_CREDS_README_PATH on first launch
FIREWALL_WHITELIST_TEMPLATE    = TEMPLATE_FILES_DIR / "firewall_whitelist.txt"      # planted as FIREWALL_WHITELIST_FILE on first launch


# ============================================================
# Host-side persistent state — everything under ~/.claude-agents
# ============================================================
# `_HOME` is the host user's home dir, captured once so paths built from it
# don't repeatedly call Path.home(). Used by AGENTS_STATE and DEFAULTING_DIRS;
# leading-underscore name marks it as paths-internal.

_HOME = Path.home()
AGENTS_STATE = _HOME / ".claude-agents"
ACCOUNT_FILE = AGENTS_STATE / ".claude.json"                       # shared OAuth account info
CREDENTIALS_FILE = AGENTS_STATE / ".credentials.json"             # shared API credentials
INSTANCES_FILE = AGENTS_STATE / "instances.toml"                 # per-instance axis store — one table per instance id: {workspace, engine, professions[], specialties[], policies[]} (tags/store.py; retired-format conversions live in tags/migrations.py)
CACHE_ROOT = AGENTS_STATE / "cache"

# {firewall} host-side caches — grouped in one dir, host-only (NOT mounted into
# containers, unlike CACHE_ROOT above). Both are TTL'd and cheaply rebuildable.
FIREWALL_CACHE_DIR = AGENTS_STATE / "firewall_cache"

# Cross-launch DNS cache. While fresh (the TTL gate lives with the cache logic
# in firewall/resolver.py), a host's cached IPs are unioned into its fresh
# resolution and rescue an outright DNS failure — never a substitute for the
# live lookup. Rewritten with each launch's fresh answers. The per-instance
# "still resolving / failed" status file is built by
# state_domain_resolve_status_path further down.
RESOLVED_DOMAINS_CACHE_FILE = FIREWALL_CACHE_DIR / "resolved_domains.txt"

# Cached CDN-provider IPv4 ranges, one file per provider under FIREWALL_CACHE_DIR
# (per-file mtime = per-provider freshness; the builder lambda lives in the
# Path-builders section below). firewall/resolver.py fetches each provider's
# published range list when its cache goes stale and falls back to a stale file
# when the fetch fails — no range data is baked into the source.

# Everything under user_extras/ is for the user to populate with
# non-project-specific configuration: extra firewall whitelist entries,
# optional cred passthroughs for cloud CLIs, etc. Grouped under one dir so
# launcher-managed state at the AGENTS_STATE root (OAuth, workspace map,
# instance dirs) doesn't mix with hand-edited files.
USER_EXTRAS_DIR = AGENTS_STATE / "user_extras"

# The {cowork} group-hosting root and everything under it are call-time BUILDERS,
# not constants — see `group_hosting_dir` in the builders section below.
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
# to the container per launch but opposite direction) or — when launching from a
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
# The operator's tmux overrides (settings/tmux.conf) inside the container. The
# generated muxer startup script sources this LAST — after the launcher's own
# options — so a user's line wins over any default. Landmark rather than a
# tmux-native path (~/.tmux.conf) on purpose: auto-loading would run BEFORE the
# launcher's options and silently lose to them, the opposite of what an
# override file promises.
TMUX_CONF_IN_CONTAINER = CLAUDE_CONFIG_IN_CONTAINER / "tmux.conf"
# The curated help text `^b ?` pops up — a PLAIN file `cat` into the popup, so
# editing it has no quoting rules at all (its printf-embedded ancestor forbade
# apostrophes and doubled every %). settings/tmux.conf's help binding names
# this path; the two ride the same mount set below.
MUXER_HELP_IN_CONTAINER = CLAUDE_CONFIG_IN_CONTAINER / "muxer-help.txt"
# Its herdr twin, popped by alt+/ (settings/herdr.toml names this path) — one
# help file per backend because the keys barely overlap.
HERDR_HELP_IN_CONTAINER = CLAUDE_CONFIG_IN_CONTAINER / "herdr-help.txt"
# The herdr backend's config, at herdr's OWN default lookup path — mounting
# there (rather than a launcher landmark + env) means zero plumbing: the
# binary just finds it. The parent dir stays writable for herdr's logs and
# sockets; only the config file itself is pinned read-only.
HERDR_CONF_IN_CONTAINER = CLAUDE_HOME_IN_CONTAINER / ".config/herdr/config.toml"
# The two host-side herdr config files. herdr reads exactly one config path
# and has no override flag, so per-shape policy means per-launch MOUNT
# selection: the shared file rides DOCKER_BASE_MOUNTS; docker_config swaps the
# solo variant (collapsed sidebar) in for {muxer}+herdr solo launches — same
# container target, which is why the variant is deliberately NOT in the base
# set (two sources may never stage one target: add_docker_mount's guard).
HERDR_CONF_SOURCE = SETTINGS_DIR / "herdr.toml"
HERDR_SOLO_CONF = SETTINGS_DIR / "herdr-solo.toml"
# The launcher-UI form manifest (tags/ui_profile.py parses it) — beside the
# muxer policy files above because its one entry today chooses between them.
# Host-side data, never mounted.
UI_FORM = SETTINGS_DIR / "ui.form"
# {cowork}'s per-instance group-hosting dir inside the container. Deliberately at
# the root rather than under CLAUDE_CONFIG_IN_CONTAINER: that path is Claude Code's
# own namespace (projects/, skills/, commands/, todos/), and the `_cowork` policy
# fragment's Stop-hook command hardcodes this path — the two must agree.
COWORK_IN_CONTAINER = Path("/cowork")
WORKSPACE_IN_CONTAINER = Path("/workspace")                        # bind-mount target for the picked workspace — the project dir every agent sees
WORKSPACES_IN_CONTAINER = Path("/workspaces")                      # cluster mode ONLY: the per-member worktrees dir. Plural because N cohabiting members share one container and so cannot each mount a different tree at /workspace — each gets /workspaces/<member-id> as its cwd instead.
CLUSTER_IN_CONTAINER = Path("/cluster")                            # cluster mode ONLY: the shared cluster dir every member sees (banner today; the message-queue later)
CLAUDE_SUMMARY_IN_CONTAINER = WORKSPACE_IN_CONTAINER / ".claude_summary"   # project summary file the agent reads on demand (lives at the workspace mount root)
BASHRC_IN_CONTAINER = CLAUDE_HOME_IN_CONTAINER / ".bashrc"         # bind-mount target for settings/bashrc.sh; also the value BASH_ENV points at so non-interactive bash sources it
INSTALL_FAILURES_LOG_IN_CONTAINER = Path("/var/log/claude-agents/install_failures.log")   # claude-owned log file each INSTALL_<TOOL> RUN in Dockerfile.code appends to on failure; docker_config.prompt_install_failures reads it post-build. Mirror of the literal path used by every Dockerfile.code install block — keep in sync (no build-arg threading yet)
RO_MOUNT_OPTION = "ro"


# ============================================================
# {firewall} container landmarks
# ============================================================
# The firewall specialty's scripts (init-firewall.sh + firewall-entrypoint.sh)
# live in its tag dir (agents/specialty/firewall/) and are bind-mounted by
# its tag.docker — no mount dict here anymore. What remains are the
# container-side landmarks the launcher must agree on with those scripts.

LOCAL_BIN_IN_CONTAINER = Path("/usr/local/bin")   # where tag.docker entrypoint scripts land; docker_config.entrypoint_chain resolves bare names against it
FIREWALL_DONE_IN_CONTAINER = Path("/var/run/init-firewall.done")   # marker init-firewall.sh touches after its rules + self-test succeed; docker_config.wait_for_firewall_applied polls it so the phase-2 updater never injects rules into a half-built firewall. Mirror of the literal in the script — test_docker_config guards the sync
INIT_FIREWALL_SH = AGENTS_DIR / "specialty" / "firewall" / "init-firewall.sh"   # host-side source of the marker literal above (the drift-guard test reads it)


# ============================================================
# Always-on container bind-mounts
# ============================================================
# Source path (on host) → container target with any docker access-mode suffix
# (`:ro`) baked in. Iterated by docker_config.set_container_mounts, which
# calls add_docker_mount per entry — same {source: target} shape as the
# _docker_mounts accumulator the function feeds. These are the static mounts
# every launch gets — all mount staging lives in Python. Per-instance mounts (the picked
# workspace + the picked instance's state dir) stage inline next to this
# iteration since their host paths are derived from the picked instance, not
# constants.

DOCKER_BASE_MOUNTS = {
    # Per-instance state files (these source constants serve other modules too — audit, agents_crud)
    ACCOUNT_FILE:                               f"{CLAUDE_HOME_IN_CONTAINER}/.claude.json",                         # shared OAuth account info
    CREDENTIALS_FILE:                           f"{CLAUDE_CONFIG_IN_CONTAINER}/.credentials.json",                  # shared API credentials — Claude Code refreshes the token in place
    # Project-bundled sources — inlined since DOCKER_BASE_MOUNTS is their only consumer
    # NOTE: custom_commands/ is deliberately NOT here. Commands are assembled per
    # instance (shared + every command the active tags declare) into
    # state_commands_dir and mounted from there by set_container_mounts — see
    # that builder's comment.
    DOCKERIZED_CLAUDE_ROOT / "custom_skills":   f"{SKILLS_IN_CONTAINER}:{RO_MOUNT_OPTION}",                        # shared skill directory — each subdir is a skill
    SETTINGS_DIR / "statusline.sh":             f"{CLAUDE_CONFIG_IN_CONTAINER}/statusline.sh:{RO_MOUNT_OPTION}",    # shared status-line script
    SETTINGS_DIR / "bashrc.sh":                 f"{BASHRC_IN_CONTAINER}:{RO_MOUNT_OPTION}",                         # sourced by every non-interactive bash via BASH_ENV
    SETTINGS_DIR / "_summary.py":               f"{CLAUDE_CONFIG_IN_CONTAINER}/_summary.py:{RO_MOUNT_OPTION}",      # backs summary_diff / summary_save_manifest in bashrc
    SETTINGS_DIR / "keybindings.json":          f"{CLAUDE_CONFIG_IN_CONTAINER}/keybindings.json:{RO_MOUNT_OPTION}", # project-wide key bindings (Shift+Enter newline, etc.)
    SETTINGS_DIR / "tmux.conf":                 f"{TMUX_CONF_IN_CONTAINER}:{RO_MOUNT_OPTION}",                      # the muxer's KEY POLICY (quit/help/layout/mouse) + user overrides — sourced last by the generated startup script, so its lines win; inert without {muxer}
    SETTINGS_DIR / "muxer-help.txt":            f"{MUXER_HELP_IN_CONTAINER}:{RO_MOUNT_OPTION}",                     # the `^b ?` popup body (tmux backend) — plain text, cat into the popup; inert without {muxer}
    SETTINGS_DIR / "herdr-help.txt":            f"{HERDR_HELP_IN_CONTAINER}:{RO_MOUNT_OPTION}",                     # the alt+/ popup body (herdr backend) — same plain-text contract; inert without {muxer}
    HERDR_CONF_SOURCE:                          f"{HERDR_CONF_IN_CONTAINER}:{RO_MOUNT_OPTION}",                     # the herdr backend's key/theme policy at herdr's default path; solo herdr launches swap in HERDR_SOLO_CONF instead (docker_config); inert without {muxer}
}


# ============================================================
# File extensions + filenames used by multiple modules
# ============================================================
# Well-defined leaf names + extensions + intra-state-dir relative paths that
# get composed against dynamic prefixes (state dirs, instance ids, etc.) at
# call sites. Centralised here so the file-path contract is in one place
# rather than scattered as magic strings.

# The name → md-path index for agents/ lives in file_access.agent_md_index —
# a directory *listing* is file-access work, not a path constant. Each
# `<agent>.md` pairs with a `<agent>.lego` build file, read by the tags
# package (engine conf resolution lives there too, via the engine/ tree).

# Full host path the optional_creds README is planted at on first launch.
OPTIONAL_CREDS_README_PATH = OPTIONAL_CREDS_DIR / "README.txt"

# Addendum text + composition lives in tags/addendums.py rather than as
# per-tag addendum files; agents_crud.install_latest_md asks it for
# the rendered section and appends it to CLAUDE.md at write time. No path
# constants needed here.

# Base image Dockerfile — at the repo root (per-layer Dockerfiles live in
# the agents/ tree: professions' own dirs + specialty-claimed `_<name>` dirs).
BASE_DOCKERFILE = DOCKERIZED_CLAUDE_ROOT / "Dockerfile"

# The cowork hub's entry script, also at the repo root (mirrors run.py /
# quick_question.py). Named here because lifecycle.ensure_hub_running spawns it
# as a detached process — a path, not an import.
COWORK_SCRIPT = DOCKERIZED_CLAUDE_ROOT / "cowork.py"


# ============================================================
# Toolchain caches — shared across [code] agents/sessions
# ============================================================
# Same relative path on host and in container; CACHE_MOUNTS pairs each host
# cache (CACHE_ROOT / rel) with its container destination (CLAUDE_HOME_IN_CONTAINER / rel).

CACHE_REL_PATHS = [
    # [code]-related
    ".cache",  # XDG cache: uv, pip, poetry, pre-commit, huggingface, torch, yarn-v1, go-build, ccache, ...
    ".cargo/registry",           # Rust crates (.crate tarballs + index)
    ".cargo/git",                # Rust git dependencies
    "go/pkg/mod",                # Go module cache
    ".npm",                      # npm (non-XDG by design)
    ".m2/repository",            # Maven local repository
    ".gradle/caches",            # Gradle dependency + build caches
    ".gem",                      # Ruby gems
    ".cpanm",                    # Perl cpanminus work dir
    ".cpan",                     # Perl CPAN classic
    ".cabal/store",              # Haskell cabal package store
    ".stack/snapshots",          # Haskell stack resolver snapshots
    ".local/share/pnpm/store",   # pnpm content-addressed store
]
# Note: playwright's default browser-binary location is `~/.cache/ms-playwright/`,
# which already lives under the `.cache` entry above — no separate mount needed.
CACHE_MOUNTS = {CACHE_ROOT / rel: CLAUDE_HOME_IN_CONTAINER / rel for rel in CACHE_REL_PATHS}


# ============================================================
# Optional credentials — host source → container target per recognised service
# ============================================================
# Each subpath under OPTIONAL_CREDS_DIR, when present on the host, gets
# bind-mounted to the matching location inside the container so the
# corresponding CLI (aws/gcloud/gh/etc.) just works. Read-write — cloud CLIs
# need to refresh tokens / write cache; presence on host is the opt-in.
# (The matching INSTALL_<TOOL> build-arg semantics — install_creds_flags in
# container_env — are spread into the container env in set_container_env.
# Creds-presence is the ONLY driver for these CLIs; the "(Edit Preferences)" form
# covers language toolchains, a disjoint set.)
#
# Value tuple: (container_mount_target, cli_name). `cli_name` is the binary
# name of the CLI installed by Dockerfile.code for this service (e.g. "kubectl"
# for the "kube" service) — surfaced into the [code] memory addendum so the
# agent knows which tools its provided creds unlock. `None` for services that
# only contribute config to an existing tool rather than installing a new one
# (ssh uses the system ssh, npmrc/pypirc tune existing npm/pip behavior).

OPTIONAL_CREDS_MOUNTS = {
    "aws":     (f"{CLAUDE_HOME_IN_CONTAINER}/.aws",                       "aws"),
    "gcloud":  (f"{CLAUDE_HOME_IN_CONTAINER}/.config/gcloud",             "gcloud"),
    "kube":    (f"{CLAUDE_HOME_IN_CONTAINER}/.kube",                      "kubectl"),
    "ssh":     (f"{CLAUDE_HOME_IN_CONTAINER}/.ssh",                       "ssh"),
    "gh":      (f"{CLAUDE_HOME_IN_CONTAINER}/.config/gh",                 "gh"),
    "glab":    (f"{CLAUDE_HOME_IN_CONTAINER}/.config/glab-cli",           "glab"),
    "jira":    (f"{CLAUDE_HOME_IN_CONTAINER}/.config/.jira",              "jira"),
    "vercel":  (f"{CLAUDE_HOME_IN_CONTAINER}/.local/share/com.vercel.cli", "vercel"),
    "railway": (f"{CLAUDE_HOME_IN_CONTAINER}/.config/railway",            "railway"),
    "npmrc":   (f"{CLAUDE_HOME_IN_CONTAINER}/.npmrc",                     None),
    "pypirc":  (f"{CLAUDE_HOME_IN_CONTAINER}/.pypirc",                    None),
    # Trailing-`/` convention: the launcher mounts the CONTENTS of this entry
    # (each top-level child) into the target dir, rather than the entry as a whole.
    "home/":   (f"{CLAUDE_HOME_IN_CONTAINER}/",                           None),
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
# (`OPTIONAL_CREDS_README_PATH`, `BASE_DOCKERFILE`, the DOCKER_BASE_MOUNTS
# values, etc.) live with their constituent constants further up the file
# rather than down here, since they don't need parameters. Net effect: every
# filesystem path the launcher touches is named in this file, just at the
# layer that earns its space.
#
# Naming convention: `_path` suffix on every builder (file or dir — the name
# describes WHAT, the type system handles HOW). Group comments call out what's
# being built.

# Per-state-dir files & subdirs (state_dir = ~/.claude-agents/<instance>/).
# `state_domain_resolve_status_path` is the per-instance status file the
# FIREWALL_NOTICE addendum points the agent at to classify a `ConnectionRefused`
# (still resolving / failed / not listed) — accepts any base dir, including
# CLAUDE_CONFIG_IN_CONTAINER for the in-container view.
state_md_path:           Callable[[Path], Path]        = lambda state_dir: state_dir / "CLAUDE.md"
state_settings_path:     Callable[[Path], Path]        = lambda state_dir: state_dir / "settings.json"   # launcher-generated (base settings + policy fragments); RO-mounted over ~/.claude/settings.json
# The instance's slash-command dir, ASSEMBLED per launch from the shared
# custom_commands/ plus every AGENTS_COMMANDS_DIR file the active tags declare,
# then RO-mounted whole over ~/.claude/commands. It replaces a direct mount of
# custom_commands/, because docker cannot create a mountpoint for a per-tag file
# inside a read-only mount — `mount: read-only file system` at container start.
# One assembled dir, one mount.
state_commands_dir:      Callable[[Path], Path]        = lambda state_dir: state_dir / "commands"
state_domain_resolve_status_path: Callable[[Path], Path] = lambda state_dir: state_dir / "domains_pending_resolve.yml"
# Per-launch input log Claude Code writes directly under the state dir (sibling
# of `projects/`, not nested with the session transcripts). `last_history_mtime`
# uses its mtime as the "last launched" signal; audit's `no_history` check
# treats absence as "instance never started".
state_history_path:      Callable[[Path], Path]        = lambda state_dir: state_dir / "history.jsonl"

# {cowork} group-hosting builders. `group_hosting_dir` is the root: one subdir per
# participating instance, each bind-mounted into that instance's container as
# COWORK_IN_CONTAINER, so the host-side hub and the agent exchange files through
# plain filesystem IO rather than `docker cp` / `docker exec`. A group's canonical
# state (session.json + conversation.md) lives in its MANAGER's copy of the group
# dir — discovery is a scan for dirs containing session.json, so there is no
# separate registry. `hub_state_path` sits at the root, deliberately outside every
# mount: agents may read their own session.json, never the hub's own bookkeeping.
#
# The root is a no-arg BUILDER rather than a constant, like `instances_dir` and for
# the same reason — but here it earns its keep twice over. Several modules outside
# this file need the root itself (to scan it, or to check containment), and a
# module that imports a CONSTANT binds it at import time: patching
# `paths.AGENTS_STATE` in a test would then redirect this file's builders while
# leaving that module pointed at the real state dir, silently. Composing every
# builder below from `group_hosting_dir()` means one patch moves the whole feature.
#
# `group_key` composes the one string that names a group in EVERY participant's
# tree — `<manager-instance>-<project-title>` — so a coworker taking part in
# several groups keeps them in sibling dirs that never collide.
#
# Every participant's tree has the same two shapes: `<group>/` is the dir that
# participant writes, and `<group>@<sender>/` is an inbox — what someone sent it,
# written only by the hub. So `cowork_inbox_path` serves BOTH directions: the
# manager's inbox holding a coworker's submission, and the coworker's inbox
# holding what the manager handed over. Its args are (owner, group, sender), not
# (manager, ...) — the owner is whoever's tree the inbox sits in.
#
# INBOX_SEPARATOR is `@` rather than `-` on purpose: a group name is
# `<manager>-<project>`, so `-` made an inbox name ambiguous with the group dir
# of a project whose title happened to end in `-<sender>`. `@` cannot occur in a
# group name (nothing composes one with it), so no inbox name can ever collide
# with a group name — which matters because both are siblings in the same dir.
# Group discovery still keys on `session.json` presence rather than on the name;
# an inbox legitimately has none.
INBOX_SEPARATOR = "@"
group_hosting_dir:       Callable[[], Path]           = lambda: AGENTS_STATE / "group_hosting"
hub_state_path:          Callable[[], Path]           = lambda: group_hosting_dir() / "hub.state.json"
hub_pid_path:            Callable[[], Path]           = lambda: group_hosting_dir() / "hub.pid"
hub_log_path:            Callable[[], Path]           = lambda: group_hosting_dir() / "hub.log"   # the detached hub's stdout/stderr — `tail -f` is the "watch the team" view
cowork_dir_path:         Callable[[str], Path]         = lambda instance: group_hosting_dir() / instance
cowork_outbox_path:      Callable[[str], Path]         = lambda instance: group_hosting_dir() / instance / "outbox"
group_key:               Callable[[str, str], str]     = lambda manager, project: f"{manager}-{project}"
cowork_group_path:       Callable[[str, str], Path]    = lambda instance, group: group_hosting_dir() / instance / group
cowork_inbox_path:       Callable[[str, str, str], Path] = lambda owner, group, sender: group_hosting_dir() / owner / f"{group}{INBOX_SEPARATOR}{sender}"
group_session_path:      Callable[[Path], Path]        = lambda group_dir: group_dir / "session.json"   # its presence is what marks a dir as a group (and its parent as the manager)
group_conversation_path: Callable[[Path], Path]        = lambda group_dir: group_dir / "conversation.md"

# Cluster builders — the cohabiting-agents mode (design record: cluster_plan.md).
# One subdir per cluster under `clusters_dir()`, holding `cluster.toml` (the
# member set + each member's tags — the analogue of instances.toml) and one state
# dir per member. Same no-arg-BUILDER discipline as `group_hosting_dir` above and
# for the same reason: patching `paths.AGENTS_STATE` in a test must move the whole
# feature, which it cannot do for a constant another module bound at import time.
#
# `cluster_worktree_path` is where the writer-safety model lands: each member gets
# its own git worktree of the shared project, so N members editing concurrently
# integrate through git instead of clobbering one checkout.
#
# NOTE the asymmetry with a solo instance, forced by cohabitation: all members
# share ONE container, so they cannot each mount a different tree at /workspace.
# Instead the worktrees dir mounts at WORKSPACES_IN_CONTAINER and each member's
# cwd is its own subdir — the path differs per member, the mount point does not.
clusters_dir:            Callable[[], Path]            = lambda: AGENTS_STATE / "clusters"
cluster_path:            Callable[[str], Path]         = lambda session: clusters_dir() / session
cluster_state_path:      Callable[[str], Path]         = lambda session: clusters_dir() / session / "cluster.toml"  # its presence is what marks a dir as a cluster
cluster_member_dir:      Callable[[str, str], Path]    = lambda session, member: clusters_dir() / session / "members" / member
cluster_worktrees_dir:   Callable[[str], Path]         = lambda session: clusters_dir() / session / "worktrees"
cluster_worktree_path:   Callable[[str, str], Path]    = lambda session, member: clusters_dir() / session / "worktrees" / member
cluster_banner_path:     Callable[[str], Path]         = lambda session: clusters_dir() / session / "banner"       # what the tmux status line renders; hub-owned later

# Per-provider CDN-range cache file under FIREWALL_CACHE_DIR (see its comment
# further up) — provider names come from firewall/resolver.py's fetcher registry.
cdn_ranges_cache_path:   Callable[[str], Path]         = lambda provider: FIREWALL_CACHE_DIR / f"{provider}.txt"

# JSONLs Claude Code writes for the /workspace project — sits at the only
# subdir Claude Code ever creates under projects/ inside this launcher
# (workspace bind-mount target is always `/workspace` → URL-encoded to
# `-workspace`). Returns the glob iterator directly; caller filters
# history.jsonl out and checks per-file size. `Path.glob` on a missing dir
# yields an empty iterator, so no existence-check needed at the call site.
state_workspace_jsonls:  Callable[[Path], Iterator[Path]] = lambda state_dir: (state_dir / "projects" / "-workspace").glob("*.jsonl")

# Instances live under ~/.claude-agents/instances/ — their own subdir keeps the
# AGENTS_STATE root uncluttered (cache/, firewall_cache/, user_extras/, the store
# file, and the OAuth files all sit at the root alongside it). No-arg builder
# so it reads AGENTS_STATE at call time — tests patch `paths.AGENTS_STATE`, and
# a constant computed at import wouldn't follow that patch.
instances_dir:           Callable[[], Path]            = lambda: AGENTS_STATE / "instances"
# Per-instance state directory itself (one level down = instances_dir() / id).
instance_state_dir_path: Callable[[str], Path]         = lambda instance: instances_dir() / instance

# Quickie ("q") — the one-shot direct-question tool. Its state lives in its own
# segregated subtree (own audit checks; never in the main picker / instances/):
# `communal/` is a single shared workspace the user drops files into, and each
# question's conversation thread gets a gibberish-named dir beside it. All
# call-time builders (patchable via paths.AGENTS_STATE), like instances_dir.
quickie_dir:                Callable[[], Path]         = lambda: AGENTS_STATE / "quickie"
quickie_communal_workspace: Callable[[], Path]         = lambda: quickie_dir() / "communal"
quickie_state_dir_path:     Callable[[str], Path]      = lambda session: quickie_dir() / session

# Per-profession toolkit profile — the user's install toggles for a
# configurable profession's optional tools (tags/toolkit_profile.py). Global,
# not per-instance: it configures the one shared image every instance of that
# profession builds from (e.g. every [code] launch reuses claude-agents:code).
toolkit_profile_path:    Callable[[str], Path]         = lambda profession: AGENTS_STATE / f"{profession}_profile.toml"
# The launcher-UI profile — profession-INDEPENDENT preferences (the muxer
# backend pick), same flat-bool shape as the toolkit profiles beside it (the
# name follows their `<x>_profile.toml` pattern although "ui" is no
# profession). A call-time builder like its siblings, so one AGENTS_STATE
# patch redirects it in tests.
ui_profile_path:         Callable[[], Path]            = lambda: AGENTS_STATE / "ui_profile.toml"

# Optional credentials per service (service ∈ OPTIONAL_CREDS_MOUNTS keys).
# `optional_creds_token_path` points at `<service>/token` — a plain-text
# secret the launcher forwards as the matching env var (see OPTIONAL_CREDS_TOKEN_ENV_VARS).
optional_creds_service_path: Callable[[str], Path]     = lambda service: OPTIONAL_CREDS_DIR / service
optional_creds_token_path:   Callable[[str], Path]     = lambda service: OPTIONAL_CREDS_DIR / service / "token"

