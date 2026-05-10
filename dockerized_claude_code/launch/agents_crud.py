"""Agent state CRUD: workspace mapping, state-dir lifecycle, the interactive
session-suffix prompt, and the picker-entry builders (creatable_agents,
continuable_instances, delete_instance, modify_instance) the menu_picker UI consumes.

Imports from agent_composition only; nothing from run.py or menu_picker — both import from here.
"""

import json
import os
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path

from .agent_composition import (
    AGENTS_DIR, AGENTS_STATE, MD_EXT, MEMORY_DIR, MODE_AUTO, MODE_DOOD, ORDERED_MODES,
    agent_sort_key, find_md_for_agent, parse_stem,
)

ACCOUNT_FILE = AGENTS_STATE / ".claude.json"
CREDENTIALS_FILE = AGENTS_STATE / ".credentials.json"
AGENT_WORKSPACE_MAP_FILE = AGENTS_STATE / "agent_workspace_map.json"
AGENT_MODES_MAP_FILE = AGENTS_STATE / "agent_modes_map.json"  # {instance_id: [mode, ...]}; only entries for instances with modes
# Where new instances default their workspace to when launched from a "neutral"
# directory (one in DEFAULTING_DIRS — typically $HOME). Acts as a shared sandbox so the
# launcher never silently bind-mounts something like the user's whole home dir.
FALLBACK_WORKSPACE = "/ai_workspace"
# Directories that divert workspace selection to FALLBACK_WORKSPACE — when launching
# from one of these (cwd ∈ DEFAULTING_DIRS), DEFAULT_WORKSPACE falls back instead of
# using $PWD. Same list also drives the picker's `(DEFAULT DIR)` mark on Cont rows
# at FALLBACK_WORKSPACE when cwd resolves to one of these dirs.
DEFAULTING_DIRS = [
    os.path.expanduser("~"),
    os.path.expanduser("~/Desktop"),
    os.path.expanduser("~/Downloads"),
    os.path.expanduser("~/Pictures"),
    os.path.expanduser("~/Videos"),
    os.path.expanduser("~/.ssh"),
    "/tmp",
    "/var/tmp",
    "/",
]
DEFAULT_WORKSPACE = (
    os.environ.get("AI_WORKSPACE")
    or (FALLBACK_WORKSPACE if os.getcwd() in DEFAULTING_DIRS else os.getcwd())
)  # fall back to $PWD, except when $PWD is one of DEFAULTING_DIRS — then use FALLBACK_WORKSPACE
SESSION_SEP = "__"
NO_WORKSPACE_DISPLAY = "?"  # subtitle placeholder for instances with no valid workspace


def instance_name(agent, session):
    """Compose the canonical state-dir id `<agent>__<session>` from a clean agent
    name + session suffix. (The (parent) conf-alias suffix is stripped before agent
    names ever reach this — see creatable_agents / parse_cli.)"""
    return f"{agent}{SESSION_SEP}{session}"


def state_dir(agent, session):
    """Path to an instance's state directory under AGENTS_STATE."""
    return AGENTS_STATE / instance_name(agent, session)


def state_md(agent, session):
    """Path to the CLAUDE.md inside an instance's state directory."""
    return state_dir(agent, session) / "CLAUDE.md"


def list_all_instances():
    """Return every `{agent}__{session}` dir under AGENTS_STATE (filesystem order;
    callers that need a specific order sort themselves)."""
    if not AGENTS_STATE.exists():
        return []
    return [d.name for d in AGENTS_STATE.iterdir() if d.is_dir() and SESSION_SEP in d.name]


def _load_json_map(path):
    """Parse a JSON-mapping file into a dict. Missing or empty files yield {}.
    Shared by load_workspace_map and load_modes_map — same shape, different paths."""
    if not path.exists():
        return {}
    content = path.read_text().strip()
    return json.loads(content) if content else {}


def _save_json_map(path, mapping):
    """Write a dict as pretty-printed JSON to `path`. Creates AGENTS_STATE if missing.
    Shared by save_workspace_map and save_modes_map — same shape, different paths."""
    AGENTS_STATE.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(mapping, indent=4, sort_keys=True) + "\n")


def load_workspace_map():           return _load_json_map(AGENT_WORKSPACE_MAP_FILE)
def save_workspace_map(mapping):    _save_json_map(AGENT_WORKSPACE_MAP_FILE, mapping)
def load_modes_map():               return _load_json_map(AGENT_MODES_MAP_FILE)
def save_modes_map(mapping):        _save_json_map(AGENT_MODES_MAP_FILE, mapping)


def update_workspace_map(instance_id, workspace):
    """Persist (instance_id → workspace) in agent_workspace_map.json. No-op if the
    entry is already set to this value — avoids rewriting the file on every cont
    relaunch when the workspace hasn't changed."""
    m = load_workspace_map()
    if m.get(instance_id) != workspace:
        m[instance_id] = workspace
        save_workspace_map(m)


def validate_stored_workspace(instance_id, workspace):
    """Exit if a stored workspace path is a non-existent directory (stale
    agent_workspace_map.json entry). None passes through so the caller can
    decide to prompt for a new value instead of treating absence as an error."""
    if workspace is not None and not Path(workspace).is_dir():
        sys.exit(
            f"Workspace for '{instance_id}' is not a valid directory: {workspace}\n"
            f"Fix the entry in {AGENT_WORKSPACE_MAP_FILE}"
        )


def get_instance_modes(instance_id):
    """Return the modes list for an instance (empty if none set)."""
    return load_modes_map().get(instance_id, [])


def warn_dood_with_auto():
    """Stern red warning when an instance ends up with both {auto} and {DooD}
    enabled. {auto} drops Claude Code's permission prompts; {DooD} hands the
    agent the host's Docker daemon. Together the agent can do effectively
    anything on the host, unattended. Blocks until the user presses any key
    so the warning isn't silently scrolled past."""
    print()
    print("\033[1;31m  ⚠ YOU'VE ENABLED BOTH {auto} AND {DooD} - PROCEED WITH CAUTION,")
    print("    AS THE AI AGENT HAS THE POWER TO DO ANYTHING ON YOUR COMPUTER,")
    print("    AND DOESN'T REQUIRE PERMISSION!\033[0m")
    print()
    print("  [press any key to continue] ", end="", flush=True)
    try:
        import termios, tty
        fd = sys.stdin.fileno()
        old = termios.tcgetattr(fd)
        try:
            tty.setcbreak(fd)        # cbreak keeps Ctrl+C working — raw would swallow it
            sys.stdin.read(1)
        finally:
            termios.tcsetattr(fd, termios.TCSADRAIN, old)
    except (ImportError, OSError):   # non-Linux/macOS or no tty → fallback requires Enter
        input()
    print()


def set_instance_modes(instance_id, modes):
    """Persist the modes list for an instance. An empty list removes the entry
    from the map (we don't store empty entries — keeps the file small and the
    'no modes' case explicit by absence). Warns on the {auto}+{DooD} combination."""
    m = load_modes_map()
    if modes:
        m[instance_id] = modes
    else:
        m.pop(instance_id, None)
    save_modes_map(m)
    if MODE_AUTO in modes and MODE_DOOD in modes:
        warn_dood_with_auto()


def resolve_pick(name):
    """Resolve a name string into a launch payload tuple matching select_agent's
    return shape. Two cases:
        '<agent>__<session>' with a state dir on disk → ('cont', {...})
        '<agent>'           with a matching .md       → ('new', {...})
    Returns None if neither matches (orphan state dir without .md, typo, etc.).

    Used by run.py's parse_cli to convert sys.argv[1] into the same (kind, payload)
    tuple the picker would have returned, so launch's downstream flow is uniform."""
    if SESSION_SEP in name and (AGENTS_STATE / name).is_dir():
        agent, _, session = name.partition(SESSION_SEP)
        md_path = find_md_for_agent(agent)
        if md_path is not None:
            return ("cont", {
                "agent_name": agent,
                "md_path": md_path,
                "session": session,
                "workspace": load_workspace_map().get(name),
            })
    md_path = find_md_for_agent(name)
    if md_path is not None:
        return ("new", {"agent_name": name, "md_path": md_path})
    return None


def install_latest_md(agent, session, md_path):
    """Copy the agent's `.md` into the state dir as CLAUDE.md (creating the dir if needed),
    and ensure the shared OAuth files exist so docker's bind-mount doesn't auto-create them
    as root. Returns the state dir."""
    sd = state_dir(agent, session)
    (sd / "projects" / "-workspace" / "memory").mkdir(parents=True, exist_ok=True)
    state_md(agent, session).write_text(md_path.read_text())
    if not ACCOUNT_FILE.exists():
        ACCOUNT_FILE.write_text("{}")
    if not CREDENTIALS_FILE.exists():
        CREDENTIALS_FILE.write_text("{}")
    return sd


def _force_remove(path, *, name=None):
    """Best-effort removal of `path` (file, symlink, or directory). Logs what's
    being removed, falls back to `sudo rm -rf` for root-owned artifacts (Docker
    bind-mount leftovers), and follows up with `sudo -k` so cached credentials
    don't linger past this single operation.

    `name` is an optional human-friendly identifier — when provided, the path
    is treated as user-initiated removal (no "stale" descriptor in the log)
    and a sudo failure pauses for keypress so the user can read the failure
    before the function returns (used by `delete_instance`, mid-picker UX).
    Without `name`, the removal is logged as "stale" cleanup and the function
    returns silently on failure (used by `sync_memory_templates`).

    Returns True on success (including "already absent"); False if even sudo
    couldn't remove the path."""
    if not path.exists() and not path.is_symlink():
        return True

    kind = "symlink" if path.is_symlink() else ("dir" if path.is_dir() else "file")
    descriptor = "" if name else "stale "
    print(f"  Removing {descriptor}{kind}: {path}")

    try:
        if path.is_symlink() or not path.is_dir():
            path.unlink(missing_ok=True)
        else:
            shutil.rmtree(path)
        return True
    except FileNotFoundError:
        return True   # raced with another removal; consider it done
    except (PermissionError, OSError):
        pass          # fall through to sudo escalation

    print(f"\n  Permission denied — root-owned (Docker bind-mount artifact). Elevating with sudo...")
    result = subprocess.run(["sudo", "rm", "-rf", str(path)], check=False)
    subprocess.run(["sudo", "-k"], check=False)   # clear cached credentials
    if result.returncode == 0:
        return True

    print(f"\n  sudo cleanup failed (exit {result.returncode}).")
    print(f"  Manual cleanup:  sudo rm -rf '{path}'")
    if name:
        input("\n  Press Enter to continue...")
    return False


def sync_memory_templates(state_path, modes):
    """Reconcile per-instance MEMORY.md with the templates whose activation
    condition is currently true. Each template's first and last lines act as
    wrapper markers that locate the block in MEMORY.md without inline marker
    comments. Anything *outside* the wrapped blocks is preserved verbatim —
    that's where agent-added auto-memory pointer entries live."""
    templates = [
        # always-active templates (one entry per filename)
        ("seek_summary.md", True),
        # mode-conditional addendums (one entry per mode in ORDERED_MODES)
        *((f"{mode.lower()}-addendum.md", mode in modes) for mode in ORDERED_MODES),
    ]
    memory_path = state_path / "projects" / "-workspace" / "memory" / "MEMORY.md"

    # Defensive cleanup for pre-existing instances. When MEMORY.md was a project-
    # wide read-only bind-mount, Docker may have left an artifact at this path —
    # a symlink, an auto-created placeholder, an empty directory, a root-owned
    # file, or in some setups even a regular file with stale content. Anything
    # other than a regular non-symlink file gets removed here so the read/write
    # below operates on a clean slate; a write-time PermissionError later (e.g.
    # a regular but root-owned leftover) triggers another removal + retry.
    if memory_path.is_symlink() or (memory_path.exists() and not memory_path.is_file()):
        _force_remove(memory_path)

    original = memory_path.read_text() if memory_path.exists() else ""
    content = original

    for filename, active in templates:
        template_file = MEMORY_DIR / filename
        if not template_file.exists():
            continue
        template = template_file.read_text().strip()   # also drops leading/trailing newlines so lines[0]/[-1] are real wrappers
        lines = template.splitlines()
        if len(lines) < 2:
            continue   # need at least a start- and end-wrapper line
        start, end = lines[0], lines[-1]

        s_idx = content.find(start)
        e_idx = content.find(end)
        in_memory = s_idx != -1 and s_idx < e_idx

        if not active and not in_memory:
            continue   # nothing to add or remove

        if in_memory:
            end_pos = e_idx + len(end)
        else:
            # Treat append as "splice into the empty range at end-of-content".
            s_idx = end_pos = len(content)

        # Walk past any newlines immediately before s_idx — keeps the leading "\n\n"
        # separator from stacking onto existing newlines (a removal then drops the
        # block's leading blank lines cleanly; an append onto trailing-newline content
        # produces exactly one "\n\n" separator instead of three).
        while s_idx > 0 and content[s_idx - 1] == "\n":
            s_idx -= 1

        # Lead with "\n\n" only when there's preceding content — keeps the first template
        # at the top of the file from getting a stray leading blank line.
        new_block = (("\n\n" if s_idx > 0 else "") + template) if active else ""
        content = content[:s_idx] + new_block + content[end_pos:]

    if content != original:
        memory_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            memory_path.write_text(content)
        except PermissionError:
            # Stale file with restrictive perms (typically a root-owned bind-mount
            # leftover that survived the cleanup above because it was a regular file).
            # Force-remove and retry; sudo fallback inside _force_remove handles the
            # root-owned case.
            _force_remove(memory_path)
            memory_path.write_text(content)


# === Picker entries — return dicts the menu_picker UI renders directly. ===

def creatable_agents():
    """Agent dicts for the picker's Create rows; sorted by model family/version. Raw
    fields only — `menu_picker` derives display values (description, preview) from md_text."""
    out = []
    for path in AGENTS_DIR.glob(f"*{MD_EXT}"):
        name, tags, _ = parse_stem(path.stem)
        if name == "default":
            continue
        out.append({
            "agent_name": name,                       # the agent's clean identifier; used everywhere downstream
            "tags": tags,                             # filename-grammar tags (e.g. ["prog"]); rendered prefixed in green by menu_picker
            "md_path": path,
            "md_text": path.read_text(),              # raw .md content; menu_picker uses it for both description and preview
        })
    out.sort(key=lambda d: agent_sort_key((d["agent_name"], d["md_path"])))
    return out


def relative_time(mtime):
    """Human-readable relative time from an epoch mtime (e.g. '3 days ago', '5 minutes ago')."""
    delta = datetime.now() - datetime.fromtimestamp(mtime)
    if delta.days >= 1:
        return f"{delta.days} day{'s' if delta.days != 1 else ''} ago"
    hours = delta.seconds // 3600
    if hours >= 1:
        return f"{hours} hour{'s' if hours != 1 else ''} ago"
    minutes = delta.seconds // 60
    return f"{minutes} minute{'s' if minutes != 1 else ''} ago" if minutes else "just now"


def _last_used_mtime(instance_id):
    """Return mtime of the latest history.jsonl under the instance state dir, or None if absent."""
    files = list((AGENTS_STATE / instance_id).rglob("history.jsonl"))
    return max((f.stat().st_mtime for f in files), default=None)


def has_continuable_history(instance_id):
    """Whether an instance has any actual conversation transcript that
    `claude --continue` can load. Claude Code writes input events to
    `projects/<encoded-workspace>/history.jsonl` (used by the picker for the
    'Last used' timestamp) regardless of whether a conversation actually
    occurred — but the conversation itself lives in a session-UUID JSONL file
    alongside it. If the only thing on disk is `history.jsonl` (or all other
    JSONLs are 0-byte), `--continue` will fail with 'No conversation found
    to continue' and exit. This check lets the launcher fall back to a fresh
    session in that case instead of crashing."""
    projects_dir = AGENTS_STATE / instance_id / "projects"
    if not projects_dir.is_dir():
        return False
    for jsonl in projects_dir.rglob("*.jsonl"):
        if jsonl.name == "history.jsonl":
            continue
        try:
            if jsonl.stat().st_size > 0:
                return True
        except OSError:
            continue
    return False


def continuable_instances():
    """Instance dicts for the picker's Cont/DELETE rows. Orphans (missing .md) skipped;
    sorted by (agent rank, session). Marks instances whose workspace resolves to the
    current working directory (for the picker's CURRENT DIR hint), and surfaces tags
    (from the .md filename) and modes (from agent_modes_map.json) so the picker can
    style the row and the modify flow can decide which prompts to show."""
    cwd = Path.cwd().resolve()
    defaulting_dirs_resolved = {Path(d).resolve() for d in DEFAULTING_DIRS}
    fallback_resolved = Path(FALLBACK_WORKSPACE).resolve()
    # True when cwd resolves to one of DEFAULTING_DIRS (symlinks normalized via .resolve(),
    # so e.g. /home/<user> matches /var/users/<user> if the latter symlinks to the former).
    # Subdirectories deliberately don't count — being in a project under $HOME doesn't make
    # /ai_workspace your "default" in any meaningful way.
    cwd_is_defaulting_dir = cwd in defaulting_dirs_resolved
    mapping = load_workspace_map()
    modes_map = load_modes_map()
    out = []
    for dir_name in list_all_instances():
        agent, _, session = dir_name.partition(SESSION_SEP)
        md_path = find_md_for_agent(agent)
        if md_path is None:
            continue
        _, tags, _ = parse_stem(md_path.stem)
        instance = instance_name(agent, session)
        modes = modes_map.get(instance, [])
        ws = mapping.get(instance)
        ws_valid = bool(ws and Path(ws).is_dir())
        ws_display = ws if ws_valid else NO_WORKSPACE_DISPLAY
        ws_resolved = Path(ws).resolve() if ws_valid else None
        is_current_dir = ws_valid and ws_resolved == cwd
        is_default_dir = ws_valid and cwd_is_defaulting_dir and ws_resolved == fallback_resolved
        last_mtime = _last_used_mtime(instance)
        last_used_display = relative_time(last_mtime) if last_mtime is not None else "(never)"
        modes_display = ", ".join(modes) if modes else "(none)"
        out.append({
            "id": instance,
            "agent_name": agent,
            "session": session,
            "md_path": md_path,
            "tags": tags,
            "modes": modes,
            "modes_display": modes_display,        # comma-joined or "(none)"; menu_picker uses it verbatim
            "workspace": ws,                       # raw — None if missing from map; may be invalid path string
            "workspace_display": ws_display,
            "is_current_dir": is_current_dir,
            "is_default_dir": is_default_dir,      # cwd ∈ DEFAULTING_DIRS and ws is FALLBACK_WORKSPACE — tagged `(DEFAULT DIR)` by menu_picker
            "state_path": AGENTS_STATE / instance, # ~/.claude-agents/<id>; shown in the preview pane
            "last_used_display": last_used_display,
        })
    out.sort(key=lambda d: (agent_sort_key((d["agent_name"], d["md_path"])), d["session"]))
    return out


def delete_instance(instance_id):
    """Remove an instance's state dir and its workspace + modes mapping entries.
    Path removal goes through `_force_remove(name=...)` which logs the removal,
    handles root-owned Docker bind-mount leftovers via sudo, and pauses for
    keypress on failure. Already-gone state dirs are treated as success so the
    map entries are still cleaned up."""
    state_path = AGENTS_STATE / instance_id
    if not _force_remove(state_path, name=instance_id):
        return   # _force_remove printed errors and waited for keypress
    m = load_workspace_map()
    if instance_id in m:
        del m[instance_id]
        save_workspace_map(m)
    m = load_modes_map()
    if instance_id in m:
        del m[instance_id]
        save_modes_map(m)


def modify_instance(old_id, agent, new_session, new_workspace, new_modes):
    """Move an instance's state dir to a new (agent, session) and update both the
    workspace and modes mappings. No-op for the rename if old and new ids match;
    the maps are always rewritten so callers can change modes without renaming."""
    new_id = instance_name(agent, new_session)
    if new_id != old_id:
        new_dir = AGENTS_STATE / new_id
        if new_dir.exists():
            raise ValueError(f"Instance '{new_id}' already exists.")
        (AGENTS_STATE / old_id).rename(new_dir)
    # workspace map
    m = load_workspace_map()
    if new_id != old_id:
        m.pop(old_id, None)
    m[new_id] = new_workspace
    save_workspace_map(m)
    # modes map — single load/save (mirrors set_instance_modes' shape inline so a
    # rename costs one file write instead of two), plus the same {auto}+{DooD}
    # warning at the end so both persistence paths surface it consistently.
    m = load_modes_map()
    if new_id != old_id:
        m.pop(old_id, None)
    if new_modes:
        m[new_id] = new_modes
    else:
        m.pop(new_id, None)
    save_modes_map(m)
    if MODE_AUTO in new_modes and MODE_DOOD in new_modes:
        warn_dood_with_auto()
