#!/usr/bin/env python3
import os, sys, subprocess, shutil, json, re
from pathlib import Path
from pick import pick  # pip install pick
from dotenv import dotenv_values  # pip install python-dotenv

PROJECT = Path(__file__).resolve().parent
AGENTS_DIR = PROJECT / "agents"

DEFAULT_CONF = AGENTS_DIR / "default.conf"
MD_EXT = ".md"
CONF_EXT = ".conf"
COMPOSE_FILE = PROJECT / "docker-compose.yml"
AGENTS_STATE = Path.home() / ".claude-agents"
ACCOUNT_FILE = AGENTS_STATE / ".claude.json"
CREDENTIALS_FILE = AGENTS_STATE / ".credentials.json"
AGENT_WORKSPACE_MAP_FILE = AGENTS_STATE / "agent_workspace_map.txt"
DEFAULT_WORKSPACE = os.environ.get("AI_WORKSPACE", "/ai_workspace")

SESSION_SEP = "__"
instance_name = lambda agent, session: f"{agent}{SESSION_SEP}{session}"
state_dir = lambda agent, session: AGENTS_STATE / instance_name(agent, session)
state_md = lambda agent, session: state_dir(agent, session) / "CLAUDE.md"


def list_sessions(agent):
    """Return session suffixes for existing {agent}__* state dirs, sorted."""
    prefix = f"{agent}{SESSION_SEP}"
    if not AGENTS_STATE.exists():
        return []
    return sorted(
        d.name[len(prefix):] for d in AGENTS_STATE.iterdir()
        if d.is_dir() and d.name.startswith(prefix)
    )


def list_all_instances():
    """Return every `{agent}__{session}` dir under AGENTS_STATE, sorted."""
    if not AGENTS_STATE.exists():
        return []
    return sorted(
        d.name for d in AGENTS_STATE.iterdir()
        if d.is_dir() and SESSION_SEP in d.name
    )


def load_workspace_map():
    """Parse agent_workspace_map.txt as a JSON object."""
    if not AGENT_WORKSPACE_MAP_FILE.exists():
        return {}
    content = AGENT_WORKSPACE_MAP_FILE.read_text().strip()
    return json.loads(content) if content else {}


def save_workspace_map(mapping):
    AGENTS_STATE.mkdir(parents=True, exist_ok=True)
    AGENT_WORKSPACE_MAP_FILE.write_text(json.dumps(mapping, indent=4, sort_keys=True) + "\n")


def parse_conf(md_path):
    """Load agent-specific .conf, falling back to default.conf only if none exists."""
    override = md_path.with_suffix(CONF_EXT)
    if override.exists():
        return dotenv_values(override)
    return dotenv_values(DEFAULT_CONF) if DEFAULT_CONF.exists() else {}


MODEL_FAMILY_RANK = {"opus": 3, "sonnet": 2, "haiku": 1}


def agent_sort_key(item):
    """Sort by family (Opus>Sonnet>Haiku), then version desc, then name asc."""
    name, path = item
    model = parse_conf(path).get("ANTHROPIC_MODEL", "")
    m = re.search(r"(opus|sonnet|haiku)-(\d+)(?:-(\d+))?", model)
    if not m:
        return (0, (0, 0), name)
    return (-MODEL_FAMILY_RANK[m.group(1)], (-int(m.group(2)), -int(m.group(3) or 0)), name)


def select_agent():
    """Combined picker: new-agent rows, continue-instance rows, and a delete submenu.
    Returns (agent, md_path, session_or_None, workspace_or_None)."""
    MARKER_NEW = "✨ Create"
    MARKER_CONT= " 🏷️ Cont."
    MARKER_DEL = "⚠️ DELETE‼️"
    while True:
        agents = tuple(sorted(
            ((p.stem, p) for p in AGENTS_DIR.glob(f"*{MD_EXT}") if p.stem != "default"),
            key=agent_sort_key,
        ))
        if not agents:
            sys.exit(f"No agents found. Create an .md file in {AGENTS_DIR}/.")

        width = max(len(name) for name, _ in agents)
        mapping = load_workspace_map()
        instance_width = max(
            (len(instance_name(name, s)) for name, _ in agents for s in list_sessions(name)),
            default=0,
        )

        entries = []
        for name, path in agents:
            desc = path.read_text().splitlines()[0].lstrip("# ").strip()
            entries.append((
                f"{MARKER_NEW}  {name:<{width}} — {desc}",
                ("new", name, path, None, None),
            ))
            for session in list_sessions(name):
                instance = instance_name(name, session)
                workspace = mapping.get(instance)
                ws_display = workspace if workspace and Path(workspace).is_dir() else "?"
                entries.append((
                    f"{MARKER_CONT}      {instance:<{instance_width}}  ( {ws_display} )",
                    ("cont", name, path, session, workspace),
                ))

        entries.append((
            f"{MARKER_DEL}  (Move onto deletions menu)",
            ("delete",),
        ))

        _, idx = pick([e[0] for e in entries], "Select an agent:", indicator="→")
        action = entries[idx][1]

        if action[0] == "delete":
            delete_menu()
            continue
        _, name, path, session, workspace = action
        return name, path, session, workspace


def delete_menu():
    """Flat picker over every `{agent}__{session}` dir. Confirms each deletion;
    stays on this screen until the user picks Back."""
    MARKER_DLET = "🗑 DELETE"
    MARKER_BACK = "🚪  Back"
    while True:
        instances = list_all_instances()
        entries = [(f"{MARKER_DLET}  {i}", i) for i in instances]
        entries.append((f"{MARKER_BACK}  (Move back to Agent Selection)", None))

        _, idx = pick(
            [e[0] for e in entries], "‼️ DELETE AGENT INSTANCES ‼️", indicator="→"
        )
        target = entries[idx][1]

        if target is None:
            return
        if input(f"Deleting '{target}' — Are you sure? [y/N]: ").strip().lower() != "y":
            continue
        shutil.rmtree(AGENTS_STATE / target)
        mapping = load_workspace_map()
        if target in mapping:
            del mapping[target]
            save_workspace_map(mapping)


def prompt_workspace(agent):
    """Prompt for a workspace path; Enter uses DEFAULT_WORKSPACE. Returns resolved absolute path."""
    while True:
        entered = input(
            f"Workspace path for new '{agent}' instance [{DEFAULT_WORKSPACE}]: "
        ).strip() or DEFAULT_WORKSPACE
        resolved = str(Path(entered).expanduser().resolve())
        if Path(resolved).is_dir():
            return resolved
        print(f"Not a directory: {resolved}")


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


def sync_state(agent, session, md_path):
    """Copy the agent .md as CLAUDE.md into the persistent state dir."""
    sd = state_dir(agent, session)
    (sd / "projects" / "-workspace" / "memory").mkdir(parents=True, exist_ok=True)
    state_md(agent, session).write_text(md_path.read_text())
    if not ACCOUNT_FILE.exists():
        ACCOUNT_FILE.write_text("{}")
    if not CREDENTIALS_FILE.exists():
        CREDENTIALS_FILE.write_text("{}")
    return sd


def ensure_image():
    """Rebuild the image."""
    print("  Building image...")
    ret = subprocess.call(["docker", "compose", "-f", str(COMPOSE_FILE), "build"])
    if ret != 0:
        sys.exit(ret)


def launch():
    """Pick an instance (agent+session), resolve workspace, sync state, exec docker compose."""
    agent, md_path, session, workspace = select_agent()
    resume_flag = ["--continue"] if session is not None else []

    if workspace is None:
        workspace = prompt_workspace(agent)         # pick workspace location
    elif not Path(workspace).is_dir():
        sys.exit(
            f"Workspace for '{instance_name(agent, session)}' is not a valid directory: {workspace}\n"
            f"Fix the entry in {AGENT_WORKSPACE_MAP_FILE}"
        )

    if session is None:
        session = prompt_session(agent, workspace)  # pick session suffix for this instance

    instance = instance_name(agent, session)
    mapping = load_workspace_map()
    if mapping.get(instance) != workspace:
        mapping[instance] = workspace
        save_workspace_map(mapping)

    os.environ["HOST_UID"] = str(os.getuid())
    os.environ["AGENT_STATE"] = str(sync_state(agent, session, md_path))
    os.environ["AGENT_NAME"] = agent
    os.environ["AGENT_SESSION"] = session
    pretty = instance.replace("-", " ").replace("__", " - ").title()
    os.environ["AGENT_STATUS_LINE"] = f"\033[36m● {pretty} \033[90m( {workspace} )\033[0m"
    os.environ["AI_WORKSPACE"] = workspace
    os.environ["ACCOUNT_FILE"] = str(ACCOUNT_FILE)
    os.environ["CREDENTIALS_FILE"] = str(CREDENTIALS_FILE)
    conf = parse_conf(md_path)
    os.environ.update(conf)
    ensure_image()
    print(f"\033]0;Claude Code — {instance}\007", end="", flush=True)
    cmd = (
        ["docker", "compose", "-f", str(COMPOSE_FILE), "run", "--rm", "-it"]
        + [item for key in conf for item in ("-e", key)]
        + ["claude-code"]
        + resume_flag
        + sys.argv[1:]
    )
    sys.exit(subprocess.call(cmd))


if __name__ == "__main__":
    launch()
