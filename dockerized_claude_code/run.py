#!/usr/bin/env python3
import argparse
import sys

from launch.agent_composition import (
    AGENTS_DIR, MODE_AUTO,
    apply_composition, compute_chain, load_conf, parse_stem,
)
from launch.agents_crud import (
    creatable_agents, get_instance_modes, has_continuable_history,
    install_latest_md, instance_name, resolve_pick, set_instance_modes,
    sync_memory_templates, update_workspace_map, validate_stored_workspace,
)
from launch.docker_config import (
    print_launch_banner, require_docker, run_compose, set_container_env,
)
from launch.menu_picker import select_agent, ask_for_workspace, prompt_modes, prompt_session
from launch.user_additions import (
    aggregated_skills_mounts, ensure_firewall_whitelist, ensure_optional_creds_readme,
    firewall_whitelist_count, optional_creds_mounts,
)

require_docker()


def parse_cli():
    """Parse the launcher's CLI. Returns (pick, claude_args):
        pick        — ('new'|'cont', payload) if a known agent/instance name was
                      given as the positional arg, else None (picker will run).
        claude_args — anything else from argv: flags argparse didn't recognize, plus
                      the positional if it didn't resolve to a known target. These
                      get appended to the `docker compose run … claude-code` command
                      so they reach claude inside the container.

    Use `--` to force args through to claude even when they look like our own flags
    (e.g. `python3 run.py poet -- --help` runs poet and passes --help to claude).

    Examples — argv[1:] split into (positional `target`, leftover `claude_args`):
        []                       → target=None,    claude_args=[]                # picker opens
        ["poet"]                 → target="poet",  claude_args=[]
        ["poet", "--print"]      → target="poet",  claude_args=["--print"]
        ["--some-flag"]          → target=None,    claude_args=["--some-flag"]   # picker opens; flag → claude
        ["poet", "--", "--help"] → target="poet",  claude_args=["--help"]        # `--` ends our parsing
        ["bogus", "extra"]       → target="bogus", claude_args=["extra"]         # bogus unresolved → moved to claude_args
    """
    parser = argparse.ArgumentParser(
        prog="run.py",
        description="Launcher for Claude Code agents. Omit the target to open the picker.",
    )
    parser.add_argument(
        "target",
        nargs="?",
        help="Agent name (e.g. 'poet') or instance id (e.g. 'poet__myproject').",
    )
    args, claude_args = parser.parse_known_args()
    if args.target is None:
        return None, claude_args
    pick = resolve_pick(args.target)
    if pick is None:
        # Unknown name — pass it through to claude as a positional, picker still runs.
        return None, [args.target] + claude_args
    return pick, claude_args


def select_pick():
    """Stage 1 — Input. Verify there are agents to pick from, parse CLI args, fall
    back to the interactive picker if no target was given on the command line, exit
    cleanly if the user cancels. Returns (kind, payload, claude_args)."""
    if not creatable_agents():
        sys.exit(f"No agents found. Create an .md file in {AGENTS_DIR}/.")
    pick, claude_args = parse_cli()
    pick = pick or select_agent()
    if pick is None:
        sys.exit(0)
    kind, payload = pick
    return kind, payload, claude_args


def resolve_target(payload):
    """Stage 2 — Filesystem validation. Unpack the payload into agent / md_path,
    ensure a workspace (prompt for new, validate-or-exit for cont's stored value),
    name a session (prompt for new), derive the canonical instance id.
    Returns (agent, md_path, session, workspace, instance)."""
    agent = payload["agent_name"]
    md_path = payload["md_path"]
    session = payload.get("session")          # set for cont, None for new (filled in by prompt_session below)
    workspace = payload.get("workspace")      # same — None for new, possibly-stale path string for cont
    if session is not None:
        validate_stored_workspace(instance_name(agent, session), workspace)
    if workspace is None:
        workspace = ask_for_workspace(agent)
    if session is None:
        session = prompt_session(agent, workspace)
    return agent, md_path, session, workspace, instance_name(agent, session)


def compute_resume_flag(kind, instance):
    """Stage 3 — Resume detection. Returns the claude args needed to resume an
    existing conversation (`["--continue"]`) or `[]` for a fresh session. Cont
    with no transcript prints a notice and starts fresh — `--continue` against
    history-only state crashes claude with 'No conversation found'."""
    if kind != "cont":
        return []
    if has_continuable_history(instance):
        return ["--continue"]
    print(f"  (Instance '{instance}' has no prior conversation; starting fresh.)")
    return []


def compose_runtime(kind, instance, md_path):
    """Stage 5 — Categorisation. Pull tags from the .md filename, resolve modes
    (prompt for new instances in priority order, load stored modes for cont),
    compute the build chain, and run handler side effects to gather runtime
    extras (volume mounts, env exports). Returns (tags, modes, chain, volume_args)."""
    tags = parse_stem(md_path.stem)[1]
    if kind == "new":
        modes = prompt_modes(tags, current_modes=[])
        set_instance_modes(instance, modes)   # warns inside if both auto+DooD are set
    else:
        modes = get_instance_modes(instance)
    try:
        chain = compute_chain(tags, modes)
        extras = apply_composition(chain)
    except (ValueError, RuntimeError) as e:
        sys.exit(f"  {e}")
    return tags, modes, chain, extras["volume_args"]


def setup_state(agent, session, workspace, md_path, modes):
    """Stage 6 — Setup. Install the agent's .md into its state dir, sync per-
    instance MEMORY.md to the current modes (adds/refreshes/removes mode
    addendums while preserving agent-added pointer entries outside the wrapped
    blocks), populate the env vars compose substitutes at build/run time, load
    the per-agent conf, and gather the skill + optional-creds bind-mounts (with
    the auto-readme touch).
    Returns (conf_path, conf, skill_mounts, cred_mounts, cred_names)."""
    state_path = install_latest_md(agent, session, md_path)
    sync_memory_templates(state_path, modes)
    set_container_env(agent, session, workspace, state_path)
    conf_path, conf = load_conf(md_path)
    skill_mounts = aggregated_skills_mounts(workspace, state_path)
    ensure_optional_creds_readme()
    ensure_firewall_whitelist()
    cred_mounts, cred_names = optional_creds_mounts()
    return conf_path, conf, skill_mounts, cred_mounts, cred_names


def launch():
    """Seven-stage orchestrator: input → filesystem validation → resume detection
    → persist → categorise (tags/modes/chain) → setup (state/env/mounts) → run.
    One call per stage so a future operation slots in at the right point with
    localised changes."""
    kind, payload, claude_args = select_pick()
    agent, md_path, session, workspace, instance = resolve_target(payload)
    resume_flag = compute_resume_flag(kind, instance)
    update_workspace_map(instance, workspace)
    tags, modes, chain, volume_args = compose_runtime(kind, instance, md_path)
    conf_path, conf, skill_mounts, cred_mounts, cred_names = setup_state(agent, session, workspace, md_path, modes)
    whitelist_count = firewall_whitelist_count() if MODE_AUTO in modes else None
    print_launch_banner(md_path, conf_path, tags, modes, skill_mounts, cred_names, whitelist_count)
    run_compose(chain, instance, claude_args, resume_flag, volume_args, skill_mounts, cred_mounts, conf)


if __name__ == "__main__":
    launch()
