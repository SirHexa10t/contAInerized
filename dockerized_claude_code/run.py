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
ACCOUNT_FILE = AGENTS_STATE / ".claude.json"
CREDENTIALS_FILE = AGENTS_STATE / ".credentials.json"

state_dir = lambda name: AGENTS_STATE / name
state_md = lambda name: state_dir(name) / "CLAUDE.md"           # custom agent instructions
state_history = lambda name: state_dir(name) / "history.jsonl"  # indicator of past session


def select_agent():
    """Discover agents, show interactive picker, return (name, md_path)."""
    agents = tuple(sorted(
        (p.stem, p) for p in AGENTS_DIR.glob(f"*{MD_EXT}")
        if p.stem != "default"
    ))
    if not agents:
        sys.exit(f"No agents found. Create an .md file in {AGENTS_DIR}/.")
    width = max(len(name) for name, _ in agents)
    labels = [
        f"{name:<{width}} — {path.read_text().splitlines()[0].lstrip('# ').strip()}"
        for name, path in agents
    ]
    _, idx = pick(labels, "Select an agent:", indicator="→")
    return agents[idx]


def parse_conf(md_path):
    """Load agent-specific .conf, falling back to default.conf only if none exists."""
    override = md_path.with_suffix(CONF_EXT)
    if override.exists():
        return dotenv_values(override)
    return dotenv_values(DEFAULT_CONF) if DEFAULT_CONF.exists() else {}


def sync_state(name, md_path):
    """Copy the agent .md as CLAUDE.md into the persistent state dir."""
    sd = state_dir(name)
    if state_history(name).exists():
        _, idx = pick(["No", "Yes"], "History found. Clear it?", indicator="→")
        if idx == 1:
            shutil.rmtree(sd)
    sd.mkdir(parents=True, exist_ok=True)

    state_md(name).write_text(md_path.read_text())
    if not ACCOUNT_FILE.exists():
        ACCOUNT_FILE.write_text("{}")
    if not CREDENTIALS_FILE.exists():
        CREDENTIALS_FILE.write_text("{}")
    return sd


def ensure_image():
    """Rebuild the image."""
    print("  Building image...")
    ret = subprocess.call(["docker", "compose", "build"])
    if ret != 0:
        sys.exit(ret)


def launch():
    """Set env vars, ensure image exists, and exec docker compose."""
    name, md_path = select_agent()
    os.environ["HOST_UID"] = str(os.getuid())
    os.environ["AGENT_STATE"] = str(sync_state(name, md_path))
    os.environ["AGENT_NAME"] = name
    os.environ["ACCOUNT_FILE"] = str(ACCOUNT_FILE)
    os.environ["CREDENTIALS_FILE"] = str(CREDENTIALS_FILE)
    conf = parse_conf(md_path)
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
    launch()
