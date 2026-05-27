"""The launcher's file-access layer — every disk-touching primitive the
launcher uses, with caching where it earns its keep. Other modules
delegate to this one for reads, writes, scans, and stat calls; they don't
do direct file I/O themselves (except for narrowly-scoped operations like
state-dir lifecycle in agents_crud, where the file ops are inseparable
from the domain logic).

Grouped by section in this file:
  - Agent file lookup (conf_path_for, load_conf) — md_path → .conf.
    Filename-stem parsing (parse_stem / parse_agent_name) lives in `utils`;
    the name → md path index (AGENT_MD_BY_NAME) lives in `agents_crud`.
  - JSON state maps load/save with cache (load/save_workspace_map,
    load/save_modes_map) — agent_workspace_map.json + agent_modes_map.json
  - Per-instance state-dir queries (has_continuable_jsonl, last_history_mtime)
    — feed InstanceIdentity properties
  - Optional credentials (present_optional_cred_services [cached],
    optional_cred_tokens)
  - User firewall whitelist (user_firewall_whitelist_lines [cached, self-plants
    from FIREWALL_WHITELIST_TEMPLATE on first read]). The {auto}-mode first-
    launch plant of firewall_whitelist.txt + the always-on plant of
    optional_creds_readme.txt live in user_additions.plant_user_extras so
    they happen at the right point in run.py's launch flow.

Caching strategy:
  - `load_conf` is LRU-cached for the launcher process lifetime — the
    creatable/continuable sort comparators would otherwise re-dotenv-parse
    per pairwise comparison. With caching, each unique (md_path) conf read
    fires exactly once per process.
  - `load_workspace_map` / `load_modes_map` cache the JSON map dicts and
    refresh the cache on every `save_*_map`. The picker's modify/delete
    flows otherwise re-read each map ~5× per launch.
  - `present_optional_cred_services` LRU-caches the present-services set
    so its two consumers (user_additions.optional_creds_mounts +
    docker_config.set_container_env's install_creds_flags / token_env_dict
    spread) don't redo the stat sweep.
  - Same-process lifetime is the cache window across all of the above —
    each `python3 run.py` invocation is a fresh process.

No build/composition logic (that's agent_modifiers_handler), no identity dataclasses
(that's structs), no arg formatting for docker compose (that's docker_config).
Imports paths + utils only — kept leaf-shaped so structs.py can depend on it
without pulling in heavier modules. agent_modifiers_handler, agents_crud, audit,
structs, user_additions, and run.py all import from here.
"""

import glob
import json
import os
import shutil
import subprocess
import time
from collections.abc import Iterator
from functools import lru_cache
from pathlib import Path
from typing import Any

from dotenv import dotenv_values  # pip install python-dotenv

from .paths import (
    ACCOUNT_FILE, AGENT_MODES_MAP_FILE, AGENT_WORKSPACE_MAP_FILE,
    CREDENTIALS_FILE, DEFAULT_CONF, FIREWALL_WHITELIST_FILE,
    FIREWALL_WHITELIST_TEMPLATE, OPTIONAL_CREDS_MOUNTS,
    OPTIONAL_CREDS_TOKEN_ENV_VARS, agent_conf_path, optional_creds_service_path,
    optional_creds_token_path, state_history_path, state_workspace_jsonls,
)
from .utils import parse_stem

# ============================================================
# Filesystem primitives — every disk-touching syscall flows through this file
# ============================================================
# Other modules call these wrappers instead of `.mkdir()` / `.unlink()` /
# `.read_text()` / `.write_text()` / `.exists()` / `.is_dir()` / etc. directly,
# so the "who can touch the filesystem" rule is locally enforceable. The
# sudo-escalation policy used for Docker-bind-mount leftovers lives in
# `force_remove` further down — it wraps `remove_path` with the fallback
# logic so callers don't have to thread sudo handling themselves.
#
# Inside this module, Path methods are called directly (we ARE the file-access
# layer); the wrappers exist for external callers.

# --- Mutations ---

def ensure_dir(path: Path) -> None:
    """`mkdir -p` equivalent: create the directory and any missing parents
    if absent; no-op if already present."""
    path.mkdir(parents=True, exist_ok=True)


def remove_path(path: Path) -> None:
    """Remove `path` — file, symlink, or directory. No-op if absent. Dispatches
    on what's at `path` so callers don't need a parallel ladder of is_dir /
    is_symlink checks. For paths that may be root-owned (Docker bind-mount
    leftovers), use `force_remove` instead — it wraps this one with a
    sudo-escalation policy."""
    if not path.exists() and not path.is_symlink():   # broken symlinks: `.exists()` returns False, so we also check `.is_symlink()` to catch them
        return
    if path.is_symlink() or not path.is_dir():
        path.unlink(missing_ok=True)
    else:
        shutil.rmtree(path)


def force_remove(path: Path, *, name: str | None = None) -> bool:
    """Best-effort removal of `path` (file, symlink, or directory). Logs what's
    being removed, falls back to `sudo rm -rf` for root-owned artifacts (Docker
    bind-mount leftovers), and follows up with `sudo -k` so cached credentials
    don't linger past this single operation.

    `name` is an optional human-friendly identifier — when provided, the path
    is treated as user-initiated removal (no "stale" descriptor in the log)
    and a sudo failure pauses for keypress so the user can read the failure
    before the function returns (used by `agents_crud.delete_instance`, mid-
    picker UX). Without `name`, the removal is logged as "stale" cleanup and
    the function returns silently on failure.

    Returns True on success (including "already absent"); False if even sudo
    couldn't remove the path."""
    if not path.exists() and not path.is_symlink():
        return True

    kind = "symlink" if path.is_symlink() else ("dir" if path.is_dir() else "file")
    descriptor = "" if name else "stale "
    print(f"  Removing {descriptor}{kind}: {path}")

    try:
        remove_path(path)
        return True
    except FileNotFoundError:
        return True   # raced with another removal; consider it done
    except (PermissionError, OSError):
        pass          # fall through to sudo escalation

    print("\n  Permission denied — root-owned (Docker bind-mount artifact). Elevating with sudo...")
    result = subprocess.run(["sudo", "rm", "-rf", str(path)], check=False)
    subprocess.run(["sudo", "-k"], check=False)   # clear cached credentials
    if result.returncode == 0:
        return True

    print(f"\n  sudo cleanup failed (exit {result.returncode}).")
    print(f"  Manual cleanup:  sudo rm -rf '{path}'")
    if name:
        input("\n  Press Enter to continue...")
    return False


def move_path(src: Path, dst: Path) -> None:
    """Rename `src` to `dst`. Works for both files and directories.
    Used by agents_crud.modify_instance to relocate state dirs after a
    session-suffix rename."""
    src.rename(dst)


def write_text(path: Path, content: str) -> None:
    """Write `content` to `path` as text (overwriting if present). Auto-creates
    the parent directory tree if missing, so callers don't need a separate
    ensure_dir call. Use ensure_dir directly only for non-write reasons
    (creating a directory that's a bind-mount target / mount point, etc.)."""
    ensure_dir(path.parent)
    path.write_text(content)


def copy_file(src: Path, dest: Path, overwrite_if_changed: bool = False) -> None:
    """Copy `src` to `dest` (content + permissions + metadata, via shutil.copy2).
    Default behaviour: no-op when `dest` already exists — preserves user edits.
    With `overwrite_if_changed=True`, reads both files and rewrites only when
    they differ — for launcher-owned templates the user shouldn't be editing
    (e.g. optional_creds_readme.txt: regenerated when the template moves on,
    but no needless rewrite + mtime bump when nothing changed). Auto-creates
    dest's parent directory tree, matching write_text's convention."""
    if dest.exists() and (not overwrite_if_changed or src.read_bytes() == dest.read_bytes()):
        return
    ensure_dir(dest.parent)
    shutil.copy2(src, dest)


def enforce_ssh_dir_perms(ssh_dir: Path) -> None:
    """Apply SSH's strict permission requirements to a directory: 700 on the
    dir itself, 600 on every regular file inside EXCEPT `*.pub` (public keys)
    and `*_hosts` (`known_hosts`, `known_hosts2`) which get 644. ssh refuses
    to read private keys whose perms aren't 600 — and refuses to load any
    config from a dir whose perms aren't 700 — so this is what the user
    would otherwise have to set by hand. No-op if `ssh_dir` doesn't exist
    or isn't a directory. Top-level only (subdirs aren't traversed). The
    `optional_creds/ssh/` dir is expected to hold *copies* (or fresh keys)
    rather than symlinks to the user's everyday ~/.ssh; chmodding here
    isn't expected to mutate their host setup."""
    if not ssh_dir.is_dir():
        return
    ssh_dir.chmod(0o700)
    for entry in ssh_dir.iterdir():
        if not entry.is_file():
            continue
        relaxed = entry.suffix == ".pub" or entry.name.endswith("_hosts")
        entry.chmod(0o644 if relaxed else 0o600)


# --- Existence + kind queries ---
# All accept Path or str (internally `Path(path)`-coerced) so callers don't
# have to think about which they're holding.

def path_exists(path: Path | str) -> bool:
    """True iff something exists at `path` (file, dir, or symlink-to-anything)."""
    return Path(path).exists()


def is_dir(path: Path | str) -> bool:
    """True iff `path` exists and is a directory."""
    return Path(path).is_dir()


def is_file(path: Path | str) -> bool:
    """True iff `path` exists and is a regular file (not a directory or symlink-to-dir)."""
    return Path(path).is_file()


def is_symlink(path: Path | str) -> bool:
    """True iff `path` is a symlink (regardless of what — or nothing — it points to)."""
    return Path(path).is_symlink()


# --- Listing + searching ---

def iter_subdirs(parent: Path) -> Iterator[Path]:
    """Yield immediate subdirectories of `parent` (filesystem order).
    Callers wanting all entries should call parent.iterdir() — but no caller
    currently does, so the filter is folded in here."""
    for entry in parent.iterdir():
        if entry.is_dir():
            yield entry


def tab_complete_paths(text_prefix: str) -> list[str]:
    """Host filesystem glob for readline tab-completion. Returns list of
    string matches (~-expanded), each with `os.sep` appended if it's a
    directory. Used by menu_picker._path_completer."""
    matches = glob.glob(os.path.expanduser(text_prefix) + "*")
    return [m + os.sep if os.path.isdir(m) else m for m in matches]


# --- Stats ---

def file_mtime(path: Path | str) -> float | None:
    """Mtime of `path` as epoch seconds, or None if it doesn't exist or
    can't be stat'd. Single point of stat-call truth so callers don't deal
    with `path.stat().st_mtime` and the OSError surface directly."""
    try:
        return Path(path).stat().st_mtime
    except OSError:
        return None


def iter_file_stats(parent: Path) -> Iterator[tuple[Path, int, float]]:
    """Yield `(path, size, mtime)` for every regular file under `parent`.
    Used by agent_modifiers_handler.prune_caches for the size+age cache walk —
    bundling the rglob + is_file filter + stat call so the caller doesn't
    juggle three filesystem operations."""
    for f in parent.rglob("*"):
        if f.is_file():
            s = f.stat()
            yield f, s.st_size, s.st_mtime


def is_file_recent(path: Path | str, max_age_seconds: float) -> bool:
    """True iff `path` exists and its mtime is within the last `max_age_seconds`.
    Missing / unreadable / stale all → False, so callers can use this as a
    single truthy 'use this cache?' gate. Backs the {auto}-mode resolved-domains
    cache TTL gate in network.py."""
    mtime = file_mtime(path)
    return mtime is not None and time.time() - mtime <= max_age_seconds


# --- Host paths ---

def resolved_path(p: Path | str) -> Path:
    """Path(p) with symlinks resolved and ~ expanded."""
    return Path(p).resolve()


def resolved_cwd() -> Path:
    """Path.cwd() with symlinks resolved — what the launcher really thinks
    its working dir is, used by the picker for cwd/workspace matching."""
    return Path.cwd().resolve()


def home_dir() -> Path:
    """User's home directory as a Path."""
    return Path.home()


def expand_user_path(s: str) -> str:
    """Expand `~` in `s` and make it absolute; returns a string. For user-typed
    workspace paths where we want the literal expanded form (not symlink-resolved)."""
    return os.path.abspath(os.path.expanduser(s))


# --- Reads ---

def read_text(path: Path) -> str:
    """Read the entire contents of `path` as a string. Raises
    FileNotFoundError if absent (no missing_ok concept here — callers
    that expect the file might not exist should either .exists()-check
    first or use one of the higher-level readers below, which have their
    own missing-file semantics)."""
    return path.read_text()


def parse_lines(path: Path) -> Iterator[str]:
    """Iterate non-empty, non-comment-only lines from `path`, with inline
    `#` comments stripped and surrounding whitespace trimmed. Suits plain
    one-token-per-line config files (e.g. the firewall whitelist).

    Raises FileNotFoundError if the file is absent — callers must ensure
    the file exists first (typically via a paired `ensure_*` helper that
    creates it from a template). The launcher's only caller goes through
    user_firewall_whitelist_lines below, which handles ensure + cache."""
    for line in read_text(path).splitlines():
        line = line.split("#", 1)[0].strip()
        if line:
            yield line


def read_json_field(path: Path | str, *keys: str) -> Any:
    """Walk `keys` into the JSON document at `path` and return the value, or
    None on any failure: file missing, unreadable, malformed JSON, missing
    key, or a non-dict mid-walk. Callers wanting an optional field handle
    None as 'not found' rather than catching exceptions themselves."""
    try:
        cur = json.loads(read_text(Path(path)))
        for k in keys:
            cur = cur[k]
        return cur
    except (OSError, json.JSONDecodeError, KeyError, TypeError):
        return None


def conf_path_for(md_path: Path) -> Path | None:
    """Locate an agent's .conf path. A '(parent)' suffix in the filename aliases
    to '<parent>.conf'; otherwise '<name>.conf'; falls back to DEFAULT_CONF, or
    None if even that's absent. Tags are ignored here. Cheap — no file body
    is read, only existence-checked — so AgentIdentity.conf_path can call it
    on every access."""
    name, _, parent = parse_stem(md_path.stem)
    specific = agent_conf_path(parent or name)
    return specific if specific.exists() else (DEFAULT_CONF if DEFAULT_CONF.exists() else None)


@lru_cache(maxsize=None)
def load_conf(md_path: Path) -> tuple[Path | None, dict]:
    """Locate and load an agent's .conf. Returns (path_or_None, values_dict).
    Path resolution lives in conf_path_for above; this wrapper adds the dotenv
    parse for the values dict.

    Cached for the launcher process's lifetime — agent_sort_key calls this
    per pairwise sort comparison in the picker (which is O(N log N) reads of
    the same set of .conf files without caching). The returned dict is SHARED
    across all callers and must not be mutated; current consumers
    (agent_sort_key, run.py's setup_state → conf_env_args) only read."""
    path = conf_path_for(md_path)
    return path, (dotenv_values(path) if path else {})


# ============================================================
# JSON state maps (workspace + modes) — cached load + save
# ============================================================
# The two JSON map files (agent_workspace_map.json, agent_modes_map.json) get
# touched in load-mutate-save patterns by every state-mutating writer in
# agents_crud (update_workspace_map, set_instance_modes, delete_instance,
# modify_instance) plus the picker-entry factories (resolve_pick,
# continuable_instances). A naive implementation reads the file on every
# call — wasteful when the picker rebuilds after a modify/delete loops back
# through 5+ load calls.
#
# Each `load_*_map` first call reads from disk and populates the
# corresponding cache; subsequent calls return the cached dict directly.
# Each `save_*_map` writes to disk and refreshes the cache with the same
# dict that was just persisted, so the cache stays valid across our own
# writes. External edits to the files mid-launch would lie — but that's
# not a supported use case (no file-locking; concurrent launchers aren't
# protected either).
#
# Callers follow the load-mutate-save pattern: `m = load_*_map(); m[k] = v;
# save_*_map(m)`. The mutation happens on the cached dict itself (since
# load_*_map returns the cached reference); save_*_map then writes the
# mutated dict and re-points the cache to the same instance. Mutations
# between load and save are visible to any other load that happens in
# between — which is fine in single-threaded Python.

_json_map_cache: dict[Path, dict[str, Any]] = {}   # single per-process cache shared by every JSON-map file


def _cached_load_json_map(path: Path) -> dict[str, Any]:
    """Load a JSON map from `path` (top-level JSON object → dict; missing or
    empty file → {}), caching the result by path. Subsequent calls return the
    cached dict by reference (so callers' in-place mutations before save_*_map
    are visible to other loaders too — see section comment above)."""
    if path not in _json_map_cache:
        if path.exists():
            content = read_text(path).strip()
            _json_map_cache[path] = json.loads(content) if content else {}
        else:
            _json_map_cache[path] = {}
    return _json_map_cache[path]


def _cached_save_json_map(path: Path, mapping: dict[str, Any]) -> None:
    """Write `mapping` to `path` as pretty-printed JSON and refresh the cache
    entry for that path. AGENTS_STATE is auto-created by write_text via the
    internal ensure_dir call."""
    write_text(path, json.dumps(mapping, indent=4, sort_keys=True) + "\n")
    _json_map_cache[path] = mapping


# Concrete shapes: workspace map is `{instance_id: workspace_path_or_None}`,
# modes map is `{instance_id: [mode, ...]}`. The narrower types help mypy at
# every call site; the `dict[str, Any]`-returning shared backend tolerates
# either via Any-compatibility.
def load_workspace_map() -> dict[str, str | None]: return _cached_load_json_map(AGENT_WORKSPACE_MAP_FILE)
def load_modes_map() -> dict[str, list[str]]:     return _cached_load_json_map(AGENT_MODES_MAP_FILE)
def save_workspace_map(mapping: dict[str, str | None]) -> None: _cached_save_json_map(AGENT_WORKSPACE_MAP_FILE, mapping)
def save_modes_map(mapping: dict[str, list[str]]) -> None:      _cached_save_json_map(AGENT_MODES_MAP_FILE, mapping)


# ============================================================
# Shared OAuth state files
# ============================================================
# Both files live in AGENTS_STATE and are bind-mounted into each container
# at launch. Claude Code refreshes the token in .credentials.json in place,
# and .claude.json holds the OAuth account info — so they must exist on the
# host before docker mounts them, or docker auto-creates them as root-owned
# directories instead of writable files.

def ensure_shared_oauth_files() -> None:
    """Idempotently touch ACCOUNT_FILE + CREDENTIALS_FILE as empty JSON
    objects so docker's bind-mount finds them as writable host files (and
    doesn't auto-create them as root-owned dirs on first launch). No-op
    when they already exist — their actual contents are managed by Claude
    Code at runtime, not by the launcher."""
    if not path_exists(ACCOUNT_FILE):
        write_text(ACCOUNT_FILE, "{}")
    if not path_exists(CREDENTIALS_FILE):
        write_text(CREDENTIALS_FILE, "{}")


# ============================================================
# Per-instance state-dir queries (helpers for InstanceIdentity properties)
# ============================================================

def has_continuable_jsonl(state_dir: Path) -> bool:
    """True iff `state_dir` has at least one non-empty session JSONL — i.e.,
    something `claude --continue` can load. `state_workspace_jsonls` points
    at `projects/-workspace/`, where only session-UUID JSONLs live —
    `history.jsonl` is a sibling of `projects/` at the state-dir root, so
    it never shows up in the iteration. InstanceIdentity.has_continuable_history
    is a thin wrapper — pulling the disk-walk out of the dataclass keeps
    structs.py a pure data layer."""
    return any(jsonl.stat().st_size > 0 for jsonl in state_workspace_jsonls(state_dir))


def last_history_mtime(state_dir: Path) -> float | None:
    """Mtime of `<state_dir>/history.jsonl` (the per-launch input log), or
    None if it doesn't exist yet. `state_history_path` encodes the layout
    fact that this file has a single deterministic location — no walk
    needed. InstanceIdentity.last_used_mtime is a thin wrapper around this
    (same reason as above)."""
    history = state_history_path(state_dir)
    return history.stat().st_mtime if history.is_file() else None


# ============================================================
# Optional credentials (~/.claude-agents/user_extras/optional_creds/)
# ============================================================

@lru_cache(maxsize=None)
def present_optional_cred_services() -> frozenset[str]:
    """Frozenset of OPTIONAL_CREDS_MOUNTS service names whose host dir is
    present. LRU-cached for the launcher process lifetime — both
    user_additions.optional_creds_mounts and docker_config.set_container_env
    (via install_creds_flags) consume it, so without dedupe each would
    independently stat-check every service (~11 × 2 = ~22 stats per launch)."""
    return frozenset(
        name for name in OPTIONAL_CREDS_MOUNTS
        if optional_creds_service_path(name).exists()
    )


def installed_cred_clis() -> str:
    """Space-joined CLI names for present optional-cred services that install
    a CLI in Dockerfile.code (cli != None in OPTIONAL_CREDS_MOUNTS). Order
    follows the OPTIONAL_CREDS_MOUNTS declaration so the addendum reads in a
    stable order across launches. Used to render the body of
    memory_addendums.CREDENTIALS_NOTICE — a no-creds environment collapses
    the body to '' and composed_addendum drops the sub-section entirely."""
    present = present_optional_cred_services()
    return " ".join(
        cli for name, (_, cli) in OPTIONAL_CREDS_MOUNTS.items()
        if cli is not None and name in present
    )


def optional_cred_tokens() -> dict[str, str]:
    """Return `{service_name: token_string}` for every service in
    OPTIONAL_CREDS_TOKEN_ENV_VARS that has a non-empty `<service>/token`
    file on the host. Tokens are stripped of leading/trailing whitespace
    (the file is expected to hold just the secret). Empty files and absent
    files are silently skipped."""
    out = {}
    for name in OPTIONAL_CREDS_TOKEN_ENV_VARS:
        token_file = optional_creds_token_path(name)
        if token_file.is_file():
            value = read_text(token_file).strip()
            if value:
                out[name] = value
    return out


# ============================================================
# User firewall whitelist (~/.claude-agents/user_extras/firewall_whitelist.txt)
# ============================================================

@lru_cache(maxsize=None)
def user_firewall_whitelist_lines() -> tuple[str, ...]:
    """Return the user's firewall_whitelist.txt parsed lines as a tuple,
    self-planting from FIREWALL_WHITELIST_TEMPLATE on first read so parse_lines
    never sees a missing file. (copy_file is a no-op when the destination
    already exists, so this stays cheap on subsequent launches; the
    user_additions.plant_user_extras call in setup_state also lands on it
    idempotently.) Cached for the launcher process lifetime, so the two
    callers — network.start_whitelist_resolution and the launch-banner count
    in menu_picker.print_launch_banner — share one read + parse instead of
    doing it independently."""
    copy_file(FIREWALL_WHITELIST_TEMPLATE, FIREWALL_WHITELIST_FILE)
    return tuple(parse_lines(FIREWALL_WHITELIST_FILE))
