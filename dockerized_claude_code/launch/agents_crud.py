"""Agent state CRUD: workspace mapping, state-dir lifecycle, the interactive
session-suffix prompt, and the picker-entry builders (creatable_agents,
continuable_instances, delete_instance, modify_instance) the menu_picker UI consumes.

Imports from agent_composition only; nothing from run.py or menu_picker — both import from here.
"""

import json
import os
import shutil
import subprocess
from datetime import datetime
from pathlib import Path

from .agent_composition import (
    AGENTS_DIR, AGENTS_STATE, MD_EXT, agent_sort_key, find_md_for_agent, parse_stem,
)

ACCOUNT_FILE = AGENTS_STATE / ".claude.json"
CREDENTIALS_FILE = AGENTS_STATE / ".credentials.json"
AGENT_WORKSPACE_MAP_FILE = AGENTS_STATE / "agent_workspace_map.json"
AGENT_MODES_MAP_FILE = AGENTS_STATE / "agent_modes_map.json"  # {instance_id: [mode, ...]}; only entries for instances with modes
DEFAULT_WORKSPACE = (
    os.environ.get("AI_WORKSPACE")
    or ("/ai_workspace" if os.getcwd() == os.path.expanduser("~") else os.getcwd())
)  # fall back to $PWD, except when $PWD is $HOME — then use the bind-mount default
SESSION_SEP = "__"
NO_WORKSPACE_DISPLAY = "?"  # subtitle placeholder for instances with no valid workspace


# instance_name expects an already-clean agent name; the (parent) suffix is stripped once in creatable_agents.
instance_name = lambda agent, session: f"{agent}{SESSION_SEP}{session}"
state_dir = lambda agent, session: AGENTS_STATE / instance_name(agent, session)
state_md = lambda agent, session: state_dir(agent, session) / "CLAUDE.md"


def list_all_instances():
    """Return every `{agent}__{session}` dir under AGENTS_STATE (filesystem order;
    callers that need a specific order sort themselves)."""
    if not AGENTS_STATE.exists():
        return []
    return [d.name for d in AGENTS_STATE.iterdir() if d.is_dir() and SESSION_SEP in d.name]


def load_workspace_map():
    """Parse agent_workspace_map.json into a dict."""
    if not AGENT_WORKSPACE_MAP_FILE.exists():
        return {}
    content = AGENT_WORKSPACE_MAP_FILE.read_text().strip()
    return json.loads(content) if content else {}


def save_workspace_map(mapping):
    """Write the workspace map as pretty-printed JSON; creates AGENTS_STATE if needed."""
    AGENTS_STATE.mkdir(parents=True, exist_ok=True)
    AGENT_WORKSPACE_MAP_FILE.write_text(json.dumps(mapping, indent=4, sort_keys=True) + "\n")


def load_modes_map():
    """Parse agent_modes_map.json into a dict of {instance_id: [mode, ...]}."""
    if not AGENT_MODES_MAP_FILE.exists():
        return {}
    content = AGENT_MODES_MAP_FILE.read_text().strip()
    return json.loads(content) if content else {}


def save_modes_map(mapping):
    """Write the modes map as pretty-printed JSON; creates AGENTS_STATE if needed."""
    AGENTS_STATE.mkdir(parents=True, exist_ok=True)
    AGENT_MODES_MAP_FILE.write_text(json.dumps(mapping, indent=4, sort_keys=True) + "\n")


def get_instance_modes(instance_id):
    """Return the modes list for an instance (empty if none set)."""
    return load_modes_map().get(instance_id, [])


def set_instance_modes(instance_id, modes):
    """Persist the modes list for an instance. An empty list removes the entry
    from the map (we don't store empty entries — keeps the file small and the
    'no modes' case explicit by absence)."""
    m = load_modes_map()
    if modes:
        m[instance_id] = modes
    else:
        m.pop(instance_id, None)
    save_modes_map(m)


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


# === Picker entries — return dicts the menu_picker UI renders directly. ===

def creatable_agents():
    """Agent dicts for the picker's Create rows; sorted by model family/version."""
    out = []
    for path in AGENTS_DIR.glob(f"*{MD_EXT}"):
        name, tags, _ = parse_stem(path.stem)
        if name == "default":
            continue
        content = path.read_text()
        out.append({
            "label_name": name,                       # what the picker renders as the row label
            "agent_name": name,                       # parallel to continuable_instances; lets launch read agent uniformly
            "tags": tags,                             # filename-grammar tags (e.g. ["prog"]); rendered prefixed in green by menu_picker
            "description": content.splitlines()[0].lstrip("# ").strip(),
            "preview": f"Create a new instance of '{name}'.\n\n--- {path.name} ---\n{content}",
            "md_path": path,
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


def continuable_instances():
    """Instance dicts for the picker's Cont/DELETE rows. Orphans (missing .md) skipped;
    sorted by (agent rank, session). Marks instances whose workspace resolves to the
    current working directory (for the picker's CURRENT DIR hint), and surfaces tags
    (from the .md filename) and modes (from agent_modes_map.json) so the picker can
    style the row and the modify flow can decide which prompts to show."""
    cwd = Path.cwd().resolve()
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
        is_current_dir = ws_valid and Path(ws).resolve() == cwd
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
            "workspace": ws,                     # raw — None if missing from map; may be invalid path string
            "workspace_display": ws_display,
            "is_current_dir": is_current_dir,
            "preview": (
                f"Continue session '{instance}'.\n\n"
                f"Agent:     {agent}\n"
                f"Session:   {session}\n"
                f"Workspace: {ws_display}\n"
                f"Modes:     {modes_display}\n"
                f"State:     {AGENTS_STATE / instance}\n"
                f"Last used: {last_used_display}\n"
            ),
        })
    out.sort(key=lambda d: (agent_sort_key((d["agent_name"], d["md_path"])), d["session"]))
    return out


def delete_instance(instance_id):
    """Remove an instance's state dir and its workspace mapping entry. If Docker
    bind-mounts left root-owned mountpoints behind (e.g. under skills/), shutil
    can't remove them as the launcher user — fall back to `sudo rm -rf` so the
    deletion completes without forcing the user out of the picker. Already-gone
    state dirs (FileNotFoundError) are treated as success so the map entry is
    still cleaned up."""
    state_path = AGENTS_STATE / instance_id
    try:
        shutil.rmtree(state_path)
    except FileNotFoundError:
        pass  # already cleaned (manually or otherwise) — proceed to map cleanup
    except PermissionError:
        print(f"\n  Some files in '{state_path}' are root-owned (Docker bind-mount artifacts).")
        print(f"  Elevating with sudo to complete the cleanup...")
        result = subprocess.run(["sudo", "rm", "-rf", str(state_path)])
        if result.returncode != 0:
            print(f"\n  sudo cleanup failed (exit {result.returncode}).")
            print(f"  Manual cleanup:  sudo rm -rf '{state_path}'")
            # Block re-entry into the picker so the user can read the failure.
            input("\n  Press Enter to return to the picker...")
            return
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
    # modes map (empty list ⇒ remove the entry; see set_instance_modes)
    m = load_modes_map()
    if new_id != old_id:
        m.pop(old_id, None)
    if new_modes:
        m[new_id] = new_modes
    else:
        m.pop(new_id, None)
    save_modes_map(m)
