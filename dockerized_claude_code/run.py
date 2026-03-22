#!/usr/bin/env python3
import os, sys, subprocess
from pathlib import Path
from pick import pick  # pip install pick
from dotenv import dotenv_values  # pip install python-dotenv

PROJECT = Path(__file__).resolve().parent
AGENTS = PROJECT / "agents"
WORKSPACE = "/ai_workspace"

AGENTS_STATE = Path.home() / ".claude-agents"
CREDENTIALS_FILE = AGENTS_STATE / "claude.json"

claude_md = lambda d: d / "CLAUDE.md"
agent_conf = lambda d: d / "agent.conf"
state_dir = lambda d: AGENTS_STATE / d.name


def discover_agents():
    """List all agent directories under agents/."""
    agents = sorted(p for p in AGENTS.iterdir() if p.is_dir())
    if not agents:
        sys.exit("No agents found. Copy the template first:\n  cp -r template/ agents/<n>")
    return agents


def agent_label(agent_dir):
    """Format 'name — heading' for the selection menu."""
    claude_md_path = claude_md(agent_dir)
    if claude_md_path.exists():
        heading = claude_md_path.read_text().splitlines()[0].lstrip("# ").strip()  # first line sans markdown heading
        return f"{agent_dir.name} — {heading}"
    return agent_dir.name


def select_agent(agents):
    """Show an interactive picker and return the chosen agent directory."""
    labels = [agent_label(a) for a in agents]
    _, idx = pick(labels, "Select an agent:", indicator="→")  # arrow-key menu
    return agents[idx]


def parse_conf(agent_dir):
    """Parse all key=value pairs from agent.conf into a dict."""
    return dotenv_values(agent_conf(agent_dir))


def sync_state(agent_dir):
    """Copy CLAUDE.md into the persistent state dir where auth tokens live."""
    sd = state_dir(agent_dir)
    sd.mkdir(parents=True, exist_ok=True)
    (claude_md(sd)).write_text(claude_md(agent_dir).read_text())
    if not CREDENTIALS_FILE.exists():
        CREDENTIALS_FILE.write_text("{}")
    return sd


def ensure_image():
    """Rebuild the image"""
    # result = subprocess.run(["docker", "compose", "images", "-q"], capture_output=True, text=True)  # returns image IDs if built
    print("  Building image...")
    ret = subprocess.call(["docker", "compose", "build"])  # streams output to terminal
    if ret != 0:
        sys.exit(ret)


def launch(agent_dir):
    """Set env vars, ensure image exists, and exec docker compose."""
    os.environ["HOST_UID"] = str(os.getuid())
    os.environ["AGENT_STATE"] = str(sync_state(agent_dir))
    os.environ["AGENT_NAME"] = agent_dir.name
    os.environ["CREDENTIALS_FILE"] = str(CREDENTIALS_FILE)
    conf = parse_conf(agent_dir) 
    os.environ.update(conf)  # load agent configurations
    ensure_image()
    print(f"\033]0;Claude Code — {agent_dir.name}\007", end="", flush=True)  # set terminal window title
    cmd = ["docker", "compose", "run", "--rm", "-it"] + [item for key in conf for item in ("-e", key)] + ["claude-code",] + sys.argv[1:]
    sys.exit(subprocess.call(cmd))  # exit with docker's return code


if __name__ == "__main__":
    assert Path(WORKSPACE).is_dir(), f"Error: {WORKSPACE} does not exist."
    selected = select_agent(discover_agents())
    launch(selected)
