#!/usr/bin/env python3
import os, sys, subprocess, shutil
from pathlib import Path
from pick import pick  # pip install pick
from dotenv import dotenv_values  # pip install python-dotenv

PROJECT = Path(__file__).resolve().parent
AGENTS_DIR = PROJECT / "agents"
WORKSPACE = "/ai_workspace"

DEFAULT_CONF = AGENTS_DIR / "default.conf"
MD_EXT = ".md"
CONF_EXT = ".conf"
AGENTS_STATE = Path.home() / ".claude-agents"
CREDENTIALS_FILE = AGENTS_STATE / "claude.json"

agent_md = lambda name: AGENTS_DIR / f"{name}{MD_EXT}"
agent_conf = lambda name: AGENTS_DIR / f"{name}{CONF_EXT}"
state_dir = lambda name: AGENTS_STATE / name
state_md = lambda name: state_dir(name) / "CLAUDE.md"
state_history = lambda name: state_dir(name) / "history.jsonl"


def discover_agents():
    """List all agent names by finding .md files in agents/."""
    agents = sorted(
        p.stem for p in AGENTS_DIR.glob(f"*{MD_EXT}")
        if p.stem != "default"
    )
    if not agents:
        sys.exit("No agents found. Create an .md file in agents/.")
    return agents


def agent_label(name):
    """Format 'name — heading' for the selection menu."""
    md = agent_md(name)
    if md.exists():
        heading = md.read_text().splitlines()[0].lstrip("# ").strip()
        return f"{name} — {heading}"
    return name


def select_agent(agents):
    """Show an interactive picker and return the chosen agent name."""
    labels = [agent_label(a) for a in agents]
    _, idx = pick(labels, "Select an agent:", indicator="→")
    return agents[idx]


def parse_conf(name):
    """Load default.conf, then overlay agent-specific .conf if it exists."""
    conf = dotenv_values(DEFAULT_CONF) if DEFAULT_CONF.exists() else {}
    override = agent_conf(name)
    if override.exists():
        conf.update(dotenv_values(override))
    return conf


def sync_state(name):
    """Copy the agent .md as CLAUDE.md into the persistent state dir."""
    sd = state_dir(name)
    if state_history(name).exists():
        _, idx = pick(["No", "Yes"], "History found. Clear it?", indicator="→")
        if idx == 1:
            shutil.rmtree(sd)
    sd.mkdir(parents=True, exist_ok=True)

    state_md(name).write_text(agent_md(name).read_text())
    if not CREDENTIALS_FILE.exists():
        CREDENTIALS_FILE.write_text("{}")
    return sd


def ensure_image():
    """Rebuild the image."""
    print("  Building image...")
    ret = subprocess.call(["docker", "compose", "build"])
    if ret != 0:
        sys.exit(ret)


def launch(name):
    """Set env vars, ensure image exists, and exec docker compose."""
    os.environ["HOST_UID"] = str(os.getuid())
    os.environ["AGENT_STATE"] = str(sync_state(name))
    os.environ["AGENT_NAME"] = name
    os.environ["CREDENTIALS_FILE"] = str(CREDENTIALS_FILE)
    conf = parse_conf(name)
    os.environ.update(conf)
    ensure_image()
    print(f"\033]0;Claude Code — {name}\007", end="", flush=True)
    cmd = (
        ["docker", "compose", "run", "--rm", "-it"]
        + [item for key in conf for item in ("-e", key)]
        + ["claude-code"]
        + sys.argv[1:]
    )
    sys.exit(subprocess.call(cmd))


if __name__ == "__main__":
    assert Path(WORKSPACE).is_dir(), f"Error: {WORKSPACE} does not exist."
    selected = select_agent(discover_agents())
    launch(selected)
