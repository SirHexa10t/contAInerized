"""The launcher's file-access layer — every disk-touching primitive the
launcher uses, with caching where it earns its keep. Other modules
delegate to this one for reads, writes, scans, and stat calls; they don't
do direct file I/O themselves (except for narrowly-scoped operations like
state-dir lifecycle in agents_crud, where the file ops are inseparable
from the domain logic).

Grouped by section in this file:
  - Filename grammar + agent file lookup (parse_stem, find_md_for_agent,
    conf_path_for, load_conf) — name → .md / .conf
  - JSON state maps load/save with cache (load/save_workspace_map,
    load/save_modes_map) — agent_workspace_map.json + agent_modes_map.json
  - Per-instance state-dir queries (has_continuable_jsonl, last_history_mtime)
    — feed InstanceIdentity properties
  - Workspace skills discovery (discover_workspace_skills,
    prepare_skill_mount_dirs)
  - Optional credentials (present_optional_cred_services [cached],
    optional_cred_tokens, ensure_optional_creds_readme — template body at
    launch/templates/optional_creds_readme.txt)
  - User firewall whitelist (ensure_firewall_whitelist — template body at
    launch/templates/firewall_whitelist.txt, firewall_whitelist_count)

Caching strategy:
  - `find_md_for_agent` and `load_conf` are LRU-cached for the launcher
    process lifetime. The picker's continuable_instances builder + the
    creatable/continuable sort comparators would otherwise hit them many
    times with the same arguments (one AGENTS_DIR glob or one dotenv read
    per call). With caching, each unique (agent_name) glob and each unique
    (md_path) conf read fires exactly once per process.
  - `load_workspace_map` / `load_modes_map` cache the JSON map dicts and
    refresh the cache on every `save_*_map`. The picker's modify/delete
    flows otherwise re-read each map ~5× per launch.
  - `present_optional_cred_services` LRU-caches the present-services set
    so optional_creds_mounts + optional_creds_install_env consume it
    without duplicate stat calls.
  - Same-process lifetime is the cache window across all of the above —
    each `python3 run.py` invocation is a fresh process.
  - Particularly helps when AGENTS_DIR sits on a remote / networked
    filesystem where the glob round-trip is non-trivial.

No build/composition logic (that's agent_composition), no identity dataclasses
(that's structs), no arg formatting for docker compose (that's docker_config).
Imports paths + utils only — kept leaf-shaped so structs.py can depend on it
without pulling in heavier modules. agent_composition, agents_crud, audit,
structs, user_additions, and run.py all import from here.
"""

import json
import re
import shutil
from functools import lru_cache
from pathlib import Path

from dotenv import dotenv_values  # pip install python-dotenv

from .paths import (
    AGENT_MODES_MAP_FILE, AGENT_WORKSPACE_MAP_FILE, AGENTS_DIR, AGENTS_STATE,
    CONF_EXT, DEFAULT_CONF, FIREWALL_WHITELIST_FILE, HISTORY_JSONL_FILENAME,
    INSTANCE_PROJECTS_RELPATH, INSTANCE_SKILLS_RELPATH, JSONL_EXT, MD_EXT,
    OPTIONAL_CREDS_DIR, OPTIONAL_CREDS_MOUNTS, OPTIONAL_CREDS_README_FILENAME,
    OPTIONAL_CREDS_TOKEN_ENV_VARS, OPTIONAL_CREDS_TOKEN_FILENAME,
    PROJECT_CUSTOM_SKILLS_DIR, SKILL_MARKER_FILENAME, TEMPLATES_DIR,
    WORKSPACE_SKILLS_DIRNAME,
)

# ============================================================
# Filesystem primitives
# ============================================================
# Every disk-touching syscall the launcher does lives here — other modules
# call these wrappers instead of `.mkdir()` / `.unlink()` / `.read_text()` /
# `.write_text()` / etc. directly, so the "who can touch the filesystem"
# rule is locally enforceable. file_access does not own removal *policy*
# (the sudo fallback + interactive press-Enter prompt for Docker-bind-mount
# leftovers lives in agents_crud._force_remove); these are just the low-level
# operations the policy uses.

# --- Mutations ---

def ensure_dir(path):
    """`mkdir -p` equivalent: create the directory and any missing parents
    if absent; no-op if already present."""
    path.mkdir(parents=True, exist_ok=True)


def ensure_parent_dir(path):
    """Same as ensure_dir but on `path.parent` — useful before writing a
    file where the parent dir might not yet exist."""
    path.parent.mkdir(parents=True, exist_ok=True)


def delete_file(path):
    """Remove a single file or symlink. No-op if absent (missing_ok=True).
    For whole directory trees, use delete_tree."""
    path.unlink(missing_ok=True)


def delete_tree(path):
    """Remove a directory and everything inside it (`rm -rf` semantics).
    Caller must ensure path is a directory; for ambiguous cases (file vs.
    dir vs. symlink) use agents_crud._force_remove, which handles the
    sudo-escalation policy too."""
    shutil.rmtree(path)


def move_path(src, dst):
    """Rename `src` to `dst`. Works for both files and directories.
    Used by agents_crud.modify_instance to relocate state dirs after a
    session-suffix rename."""
    src.rename(dst)


def write_text(path, content):
    """Write `content` to `path` as text (overwriting if present). Auto-creates
    the parent directory tree if missing, so callers don't need a separate
    ensure_parent_dir call. Use ensure_dir directly only for non-write reasons
    (creating a directory that's a bind-mount target / mount point, etc.)."""
    ensure_parent_dir(path)
    path.write_text(content)


def copy_file(src, dest, overwrite_if_dest=False):
    """Copy `src` to `dest` (content + permissions + metadata, via shutil.copy2).
    By default, no-op when `dest` already exists — pass overwrite_if_dest=True
    when the caller wants a fresh copy each time (e.g. install_latest_md's
    per-launch refresh of the agent's CLAUDE.md). Auto-creates dest's parent
    directory tree, matching write_text's convention; callers don't need a
    separate ensure_parent_dir step."""
    if dest.exists() and not overwrite_if_dest:
        return
    ensure_parent_dir(dest)
    shutil.copy2(src, dest)


# --- Reads ---

def read_text(path):
    """Read the entire contents of `path` as a string. Raises
    FileNotFoundError if absent (no missing_ok concept here — callers
    that expect the file might not exist should either .exists()-check
    first or use one of the higher-level readers below, which have their
    own missing-file semantics)."""
    return path.read_text()


def parse_lines(path):
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


def read_json_field(path, *keys):
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


def load_json_map(path):
    """Parse a JSON-mapping file into a dict. Missing or empty files yield
    {}. Used for state-file readers where the top-level shape is a single
    JSON object (e.g. agent_workspace_map.json, agent_modes_map.json) —
    distinct from read_json_field above, which walks into a nested field."""
    if not path.exists():
        return {}
    content = read_text(path).strip()
    return json.loads(content) if content else {}


def parse_stem(stem):
    """Parse a filename stem into (name, tags, parent).

    Grammar: <name>(<bracketed-tag>|<parenthesized-parent>)*
      - `[tag]` accumulates into tags (list, in the order they appear).
      - `(parent)` is single-valued; if repeated, last wins.
      - Order between brackets and parens is free: 'name[prog](thinker)' and
        'name(thinker)[prog]' both parse the same way.

    Examples:
        'name'                → ('name', [], None)
        'name(thinker)'       → ('name', [], 'thinker')
        'name[prog]'          → ('name', ['prog'], None)
        'name[prog](thinker)' → ('name', ['prog'], 'thinker')
        'name[a][b]'          → ('name', ['a', 'b'], None)
    """
    m = re.match(r"^([^()\[\]]+)", stem)
    if not m:
        return (stem, [], None)
    name = m.group(1)
    tags = []
    parent = None
    for paren, bracket in re.findall(r"\(([^()]+)\)|\[([^\[\]]+)\]", stem[len(name):]):
        if paren:
            parent = paren
        else:
            tags.append(bracket)
    return (name, tags, parent)


@lru_cache(maxsize=None)
def find_md_for_agent(agent_name):
    """Locate an agent's .md by its clean name; handles any [tag]/(parent) suffix combination
    in the filename. Glob can't express the new grammar (`[` is a glob metacharacter), so we
    enumerate `.md` files and match on the parsed stem — cheap with the project's agent count.

    Cached for the launcher process's lifetime — the picker's continuable_instances
    builder calls this once per state dir (so without the cache, every cont row
    would re-glob AGENTS_DIR), and the sort callbacks fire it through identity
    properties many more times during pairwise comparisons."""
    for path in AGENTS_DIR.glob(f"*{MD_EXT}"):
        if parse_stem(path.stem)[0] == agent_name:
            return path
    return None


def conf_path_for(md_path):
    """Locate an agent's .conf path. A '(parent)' suffix in the filename aliases
    to '<parent>.conf'; otherwise '<name>.conf'; falls back to DEFAULT_CONF, or
    None if even that's absent. Tags are ignored here. Cheap — no file body
    is read, only existence-checked — so AgentIdentity.conf_path can call it
    on every access."""
    name, _, parent = parse_stem(md_path.stem)
    specific = AGENTS_DIR / f"{(parent or name)}{CONF_EXT}"
    return specific if specific.exists() else (DEFAULT_CONF if DEFAULT_CONF.exists() else None)


@lru_cache(maxsize=None)
def load_conf(md_path):
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
# continuable_instances) and InstanceIdentity.stored_modes. A naive
# implementation reads the file on every call — wasteful when the picker
# rebuilds after a modify/delete loops back through 5+ load calls.
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

_workspace_map_cache = None
_modes_map_cache = None


def load_workspace_map():
    """Return the agent_workspace_map.json contents as a dict, populating
    the module-private cache on first call. See section comment above for
    cache semantics."""
    global _workspace_map_cache
    if _workspace_map_cache is None:
        _workspace_map_cache = load_json_map(AGENT_WORKSPACE_MAP_FILE)
    return _workspace_map_cache


def load_modes_map():
    """Return the agent_modes_map.json contents as a dict, populating the
    module-private cache on first call. See section comment above for
    cache semantics."""
    global _modes_map_cache
    if _modes_map_cache is None:
        _modes_map_cache = load_json_map(AGENT_MODES_MAP_FILE)
    return _modes_map_cache


def _save_json_map(path, mapping):
    """Write a dict as pretty-printed JSON to `path`. Shared body for
    save_workspace_map and save_modes_map below — same shape, different paths.
    AGENTS_STATE is auto-created by write_text via ensure_parent_dir."""
    write_text(path, json.dumps(mapping, indent=4, sort_keys=True) + "\n")


def save_workspace_map(mapping):
    """Persist the workspace map to disk and refresh the cache to match."""
    global _workspace_map_cache
    _save_json_map(AGENT_WORKSPACE_MAP_FILE, mapping)
    _workspace_map_cache = mapping


def save_modes_map(mapping):
    """Persist the modes map to disk and refresh the cache to match."""
    global _modes_map_cache
    _save_json_map(AGENT_MODES_MAP_FILE, mapping)
    _modes_map_cache = mapping


# ============================================================
# Per-instance state-dir queries (helpers for InstanceIdentity properties)
# ============================================================

def has_continuable_jsonl(state_dir):
    """True iff `state_dir` has at least one non-empty session JSONL — i.e.,
    something `claude --continue` can load. Excludes `history.jsonl` (the per-
    turn input log that exists even when no real conversation happened). The
    InstanceIdentity.has_continuable_history property is a thin wrapper around
    this; pulling the disk-walk out of the dataclass keeps structs.py a pure
    data layer."""
    projects_dir = state_dir / INSTANCE_PROJECTS_RELPATH
    if not projects_dir.is_dir():
        return False
    for jsonl in projects_dir.rglob(f"*{JSONL_EXT}"):
        if jsonl.name == HISTORY_JSONL_FILENAME:
            continue
        try:
            if jsonl.stat().st_size > 0:
                return True
        except OSError:
            continue
    return False


def last_history_mtime(state_dir):
    """Mtime of the most-recently-written history.jsonl under `state_dir`, or
    None if no history file exists yet. InstanceIdentity.last_used_mtime is
    a thin wrapper around this (same reason as above)."""
    files = list(state_dir.rglob(HISTORY_JSONL_FILENAME))
    return max((f.stat().st_mtime for f in files), default=None)


# ============================================================
# User-contributed skills (custom_skills/ + <workspace>/.skills/)
# ============================================================

def discover_workspace_skills(workspace):
    """Walk PROJECT_CUSTOM_SKILLS_DIR (project-bundled) and
    `<workspace>/<WORKSPACE_SKILLS_DIRNAME>` (per-workspace) for subdirectories
    containing a SKILL.md. Returns `{name: source_path}`; when the same name
    appears in both sources, the workspace's wins (last-write)."""
    skills = {}
    for source_dir in (PROJECT_CUSTOM_SKILLS_DIR, Path(workspace) / WORKSPACE_SKILLS_DIRNAME):
        if not source_dir.is_dir():
            continue
        for skill in source_dir.iterdir():
            if skill.is_dir() and (skill / SKILL_MARKER_FILENAME).is_file():
                skills[skill.name] = skill   # workspace overrides project-bundled
    return skills


def prepare_skill_mount_dirs(state_path, names):
    """Pre-create `<state_path>/skills/<name>` on the host for each entry in
    `names` so Docker doesn't auto-create them as root (which would otherwise
    leave undeletable dirs blocking `delete_instance`'s rmtree)."""
    skills_root = state_path / INSTANCE_SKILLS_RELPATH
    for name in names:
        ensure_dir(skills_root / name)


# ============================================================
# Optional credentials (~/.claude-agents/user_extras/optional_creds/)
# ============================================================

@lru_cache(maxsize=None)
def present_optional_cred_services():
    """Frozenset of OPTIONAL_CREDS_MOUNTS service names whose host dir is
    present. LRU-cached for the launcher process lifetime — both
    optional_creds_mounts and optional_creds_install_env consume it, so
    without dedupe each would independently stat-check every service
    (~11 × 2 = ~22 stats per launch)."""
    return frozenset(
        name for name in OPTIONAL_CREDS_MOUNTS
        if (OPTIONAL_CREDS_DIR / name).exists()
    )


def optional_cred_tokens():
    """Return `{service_name: token_string}` for every service in
    OPTIONAL_CREDS_TOKEN_ENV_VARS that has a non-empty `<service>/token`
    file on the host. Tokens are stripped of leading/trailing whitespace
    (the file is expected to hold just the secret). Empty files and absent
    files are silently skipped."""
    out = {}
    for name in OPTIONAL_CREDS_TOKEN_ENV_VARS:
        token_file = OPTIONAL_CREDS_DIR / name / OPTIONAL_CREDS_TOKEN_FILENAME
        if token_file.is_file():
            value = read_text(token_file).strip()
            if value:
                out[name] = value
    return out


def ensure_optional_creds_readme():
    """Create ~/.claude-agents/user_extras/optional_creds/ + a README on first
    launch, so users who discover the directory know what to put in it. The
    README body lives at launch/templates/optional_creds_readme.txt — edit it
    there, not here. Idempotent: won't overwrite user edits to the planted
    copy. The destination directory tree is auto-created by write_text via
    ensure_parent_dir."""
    readme = OPTIONAL_CREDS_DIR / OPTIONAL_CREDS_README_FILENAME
    if not readme.exists():
        copy_file(TEMPLATES_DIR / "optional_creds_readme.txt", readme)


# ============================================================
# User firewall whitelist (~/.claude-agents/user_extras/firewall_whitelist.txt)
# ============================================================

def ensure_firewall_whitelist():
    """Create ~/.claude-agents/user_extras/firewall_whitelist.txt with a
    commented preamble on first launch so users discovering the file know
    what to put in it. The preamble lives at launch/templates/firewall_whitelist.txt
    — edit it there, not here. Idempotent: won't overwrite user edits to the
    planted copy. The destination's parent directory is auto-created by
    write_text via ensure_parent_dir."""
    if not FIREWALL_WHITELIST_FILE.exists():
        copy_file(TEMPLATES_DIR / "firewall_whitelist.txt", FIREWALL_WHITELIST_FILE)


@lru_cache(maxsize=None)
def user_firewall_whitelist_lines():
    """Return the user's firewall_whitelist.txt parsed lines as a tuple,
    ensuring the file exists first (creates from template if absent — so
    parse_lines below never sees a missing file). Cached for the launcher
    process lifetime, so the two callers — resolved_whitelist_domains in
    agent_composition and firewall_whitelist_count below — share one read
    + parse instead of doing it independently."""
    ensure_firewall_whitelist()
    return tuple(parse_lines(FIREWALL_WHITELIST_FILE))


def firewall_whitelist_count():
    """Count active entries in the user's firewall_whitelist.txt — for the
    launch banner. Excludes built-ins and the auto-added apex/www
    counterparts; this is 'how many entries did the user write themselves'."""
    return len(user_firewall_whitelist_lines())
