"""Agent domain layer: discovery, naming, workspace mapping, conf loading, sort policy,
state-dir sync, the interactive session-suffix prompt, and the picker-entry builders
(creatable_agents, continuable_instances, delete_instance) the menu_picker UI consumes.

Imports nothing from run.py or menu_picker.py — both import from here.
"""

import json
import os
import re
import shutil
from datetime import datetime
from pathlib import Path

from dotenv import dotenv_values  # pip install python-dotenv

PROJECT = Path(__file__).resolve().parent.parent  # this file lives in launch/, project root is one up
AGENTS_DIR = PROJECT / "agents"

DEFAULT_CONF = AGENTS_DIR / "default.conf"
MD_EXT = ".md"
CONF_EXT = ".conf"
AGENTS_STATE = Path.home() / ".claude-agents"
ACCOUNT_FILE = AGENTS_STATE / ".claude.json"
CREDENTIALS_FILE = AGENTS_STATE / ".credentials.json"
AGENT_WORKSPACE_MAP_FILE = AGENTS_STATE / "agent_workspace_map.txt"
DEFAULT_WORKSPACE = (
    os.environ.get("AI_WORKSPACE")
    or ("/ai_workspace" if os.getcwd() == os.path.expanduser("~") else os.getcwd())
)  # fall back to $PWD, except when $PWD is $HOME — then use the bind-mount default
SESSION_SEP = "__"
MODEL_FAMILY_RANK = {"opus": 3, "sonnet": 2, "haiku": 1}
NO_WORKSPACE_DISPLAY = "?"  # subtitle placeholder for instances with no valid workspace


def _parse_stem(stem):
    """'name(parent)' → ('name', 'parent'); 'name' → ('name', None)."""
    m = re.match(r"^(.+?)\(([^)]+)\)$", stem)
    return (m.group(1), m.group(2)) if m else (stem, None)


def find_md_for_agent(agent_name):
    """Locate an agent's .md by its clean name; handles both '<name>.md' and '<name>(*).md'."""
    direct = AGENTS_DIR / f"{agent_name}{MD_EXT}"
    if direct.exists():
        return direct
    return next(AGENTS_DIR.glob(f"{agent_name}(*){MD_EXT}"), None)


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
    """Parse agent_workspace_map.txt as a JSON object."""
    if not AGENT_WORKSPACE_MAP_FILE.exists():
        return {}
    content = AGENT_WORKSPACE_MAP_FILE.read_text().strip()
    return json.loads(content) if content else {}


def save_workspace_map(mapping):
    """Write the workspace map as pretty-printed JSON; creates AGENTS_STATE if needed."""
    AGENTS_STATE.mkdir(parents=True, exist_ok=True)
    AGENT_WORKSPACE_MAP_FILE.write_text(json.dumps(mapping, indent=4, sort_keys=True) + "\n")


def load_conf(md_path):
    """Locate and load an agent's .conf. Returns (path_or_None, values_dict).
    '<name>(parent).md' aliases to '<parent>.conf'; else '<name>.conf'; falls back to DEFAULT_CONF."""
    name, parent = _parse_stem(md_path.stem)
    specific = AGENTS_DIR / f"{(parent or name)}{CONF_EXT}"
    conf_path = specific if specific.exists() else (DEFAULT_CONF if DEFAULT_CONF.exists() else None)
    return conf_path, (dotenv_values(conf_path) if conf_path else {})


def parse_model_id(model):
    """Extract (family, major, minor) from a model ID like 'claude-opus-4-7'.
    Returns None when no recognized family is present."""
    m = re.search(r"(opus|sonnet|haiku)-(\d+)(?:-(\d+))?", model)
    if not m:
        return None
    return m.group(1), int(m.group(2)), int(m.group(3) or 0)


def agent_sort_key(item):
    """Sort by family (Opus>Sonnet>Haiku), then version desc, then name asc."""
    name, path = item
    _, conf = load_conf(path)
    parsed = parse_model_id(conf.get("ANTHROPIC_MODEL", ""))
    if parsed is None:
        return (0, (0, 0), name)
    family, major, minor = parsed
    return (-MODEL_FAMILY_RANK[family], (-major, -minor), name)


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


def prompt_session(agent, workspace):
    """Prompt for a session suffix; default = last segment of the workspace path.
    Rejects collisions with existing `{agent}__{suffix}` state dirs."""
    default = Path(workspace).name
    while True:
        suffix = input(f"Session suffix for '{agent}' [{default}]: ").strip() or default
        if not suffix:
            print("Session suffix cannot be empty.")
            continue
        if state_dir(agent, suffix).exists():
            print(f"Instance '{instance_name(agent, suffix)}' already exists. Pick another name.")
            continue
        return suffix


# === Picker entries — return dicts the menu_picker UI renders directly. ===

def creatable_agents():
    """Agent dicts for the picker's Create rows; sorted by model family/version."""
    out = []
    for path in AGENTS_DIR.glob(f"*{MD_EXT}"):
        name = _parse_stem(path.stem)[0]
        if name == "default":
            continue
        content = path.read_text()
        out.append({
            "label_name": name,                       # what the picker renders as the row label
            "agent_name": name,                       # parallel to continuable_instances; lets launch read agent uniformly
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
    current working directory (for the picker's CURRENT DIR hint)."""
    cwd = Path.cwd().resolve()
    mapping = load_workspace_map()
    out = []
    for dir_name in list_all_instances():
        agent, _, session = dir_name.partition(SESSION_SEP)
        md_path = find_md_for_agent(agent)
        if md_path is None:
            continue
        instance = instance_name(agent, session)
        ws = mapping.get(instance)
        ws_valid = bool(ws and Path(ws).is_dir())
        ws_display = ws if ws_valid else NO_WORKSPACE_DISPLAY
        is_current_dir = ws_valid and Path(ws).resolve() == cwd
        last_mtime = _last_used_mtime(instance)
        last_used_display = relative_time(last_mtime) if last_mtime is not None else "(never)"
        out.append({
            "id": instance,
            "agent_name": agent,
            "session": session,
            "md_path": md_path,
            "workspace": ws,                     # raw — None if missing from map; may be invalid path string
            "workspace_display": ws_display,
            "is_current_dir": is_current_dir,
            "preview": (
                f"Continue session '{instance}'.\n\n"
                f"Agent:     {agent}\n"
                f"Session:   {session}\n"
                f"Workspace: {ws_display}\n"
                f"State:     {AGENTS_STATE / instance}\n"
                f"Last used: {last_used_display}\n"
            ),
        })
    out.sort(key=lambda d: (agent_sort_key((d["agent_name"], d["md_path"])), d["session"]))
    return out


def delete_instance(instance_id):
    """Remove an instance's state dir and its workspace mapping entry."""
    shutil.rmtree(AGENTS_STATE / instance_id)
    m = load_workspace_map()
    if instance_id in m:
        del m[instance_id]
        save_workspace_map(m)


def redefine_instance(old_id, agent, new_session, new_workspace):
    """Move an instance's state dir to a new (agent, session) and update its workspace mapping.
    No-op for the rename if old and new ids match; the workspace map is always updated."""
    new_id = instance_name(agent, new_session)
    if new_id != old_id:
        new_dir = AGENTS_STATE / new_id
        if new_dir.exists():
            raise ValueError(f"Instance '{new_id}' already exists.")
        (AGENTS_STATE / old_id).rename(new_dir)
    m = load_workspace_map()
    if new_id != old_id:
        m.pop(old_id, None)
    m[new_id] = new_workspace
    save_workspace_map(m)
