#!/usr/bin/env python3
import argparse
import dataclasses
import sys

from launch.agent_composition import compose_chain
from launch.agents_crud import (
    install_latest_md, resolve_pick, set_instance_modes,
    update_workspace_map,
)
from launch.compose_env import set_container_env
from launch.docker_config import (
    require_docker, run_compose, set_container_mounts,
)
from launch.file_access import ensure_shared_oauth_files, load_conf
from launch.menu_picker import (
    ask_for_workspace, print_launch_banner, prompt_modes, prompt_session, select_agent,
)
from launch.paths import AGENT_MD_BY_NAME, AGENTS_DIR
from launch.structs import AgentIdentity, InstanceIdentity, SessionIdentity
from launch.user_additions import (
    optional_creds_mounts, plant_user_extras,
)


def parse_cli() -> tuple[AgentIdentity | SessionIdentity | None, list[str], bool]:
    """Parse the launcher's CLI. Returns (picked, claude_args, dry_run):
        picked      — AgentIdentity (new) | SessionIdentity (cont, is_brand_new=False)
                      if a known agent/instance name was given as the positional arg,
                      else None (picker will run).
        claude_args — anything else from argv: flags argparse didn't recognize, plus
                      the positional if it didn't resolve to a known target. These
                      get appended to the `docker compose run … claude-code` command
                      so they reach claude inside the container.
        dry_run     — `--dry-run` flag. When True, launch() runs every stage
                      (state setup, mount staging, etc.) but skips the final
                      `docker compose run`. Lets tests exercise the launch
                      pipeline end-to-end without actually building / running
                      the container.

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
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Run all state setup but skip the final `docker compose run` step.",
    )
    args, claude_args = parser.parse_known_args()
    if args.target is None:
        return None, claude_args, args.dry_run
    picked = resolve_pick(args.target)
    if picked is None:
        # Unknown name — pass it through to claude as a positional, picker still runs.
        return None, [args.target] + claude_args, args.dry_run
    return picked, claude_args, args.dry_run


def select_pick() -> tuple[AgentIdentity | SessionIdentity, list[str], bool]:
    """Stage 1 — Input. Verify there are agents to pick from, parse CLI args, fall
    back to the interactive picker if no target was given on the command line, exit
    cleanly if the user cancels. Returns (picked, claude_args, dry_run) — `picked`
    is an AgentIdentity for new and a SessionIdentity for cont. The new/cont
    distinction is encoded in the returned type (and downstream in
    inst_id.is_brand_new), so no parallel kind string is threaded alongside.
    `dry_run` comes straight off the CLI flag — see parse_cli."""
    if not AGENT_MD_BY_NAME:
        sys.exit(f"No agents found. Create an .md file in {AGENTS_DIR}/.")
    picked, claude_args, dry_run = parse_cli()
    picked = picked or select_agent()
    if picked is None:
        sys.exit(0)
    return picked, claude_args, dry_run


def resolve_target(picked: AgentIdentity | InstanceIdentity) -> InstanceIdentity:
    """Stage 2 — Filesystem validation. Promote the picker-supplied identity to a
    full InstanceIdentity: for new `picked` is an AgentIdentity, so prompt for
    workspace and session (and stamp is_brand_new=True); for cont it's already a
    SessionIdentity (stored workspace + modes + is_brand_new=False baked in), so
    just validate the workspace and pass it through (re-prompting if the map
    entry was missing). Modes resolution happens later in compose_runtime;
    launch() promotes via with_modes() afterward."""
    if isinstance(picked, InstanceIdentity):       # cont — workspace + session + is_brand_new already set
        picked.validate_workspace()
        if picked.workspace is None:               # stale / missing map entry — re-prompt and keep the SessionIdentity subclass
            return dataclasses.replace(picked, workspace=ask_for_workspace(picked.agent))
        return picked
    # new — AgentIdentity only; prompt workspace then session, stamp is_brand_new
    workspace = ask_for_workspace(picked.agent)
    session = prompt_session(picked.agent, workspace)
    return InstanceIdentity(agent=picked.agent, session=session, workspace=workspace, is_brand_new=True)


def compute_resume_flag(inst_id: InstanceIdentity) -> list[str]:
    """Stage 3 — Resume detection. Returns the claude args needed to resume an
    existing conversation (`["--continue"]`) or `[]` for a fresh session. Cont
    with no transcript prints a notice and starts fresh — `--continue` against
    history-only state crashes claude with 'No conversation found'."""
    if inst_id.is_brand_new:
        return []
    if inst_id.has_continuable_history:
        return ["--continue"]
    print(f"  (Instance '{inst_id.instance}' has no prior conversation; starting fresh.)")
    return []


def compose_runtime(inst_id: InstanceIdentity) -> tuple[SessionIdentity, list[str]]:
    """Stage 5 — Categorisation. Resolve modes (prompt for new instances in
    priority order, load stored modes for cont), promote to a SessionIdentity,
    compute the build chain, and run handler side effects (env-var staging
    + bind-mount staging via the docker_config accumulators, plus {auto}-
    mode firewall resolve kickoff). Takes an InstanceIdentity — tags come
    off it directly (.tags property), is_brand_new tells us which branch
    to take. compose_chain takes the resulting SessionIdentity directly
    (it needs tags + modes + state_dir + .chain). Returns (sess_id, chain)."""
    if inst_id.is_brand_new:
        modes = prompt_modes(inst_id.tags, current_modes=())
        set_instance_modes(inst_id.with_modes(modes))   # warns inside if both auto+DooD are set
    else:
        modes = inst_id.stored_modes
    sess_id = inst_id.with_modes(modes)
    try:
        chain = compose_chain(sess_id)
    except (ValueError, RuntimeError) as e:
        sys.exit(f"  {e}")
    return sess_id, chain


def setup_state(sess_id: SessionIdentity) -> tuple[dict[str, str], list[str]]:
    """Stage 6 — Setup. Install the agent's `.md` plus the active-chain
    addendum section into its state dir as CLAUDE.md (a single overwrite —
    install_latest_md keys off sess_id.chain for the addendums), ensure
    shared OAuth state files exist so docker doesn't auto-create them as
    root, populate the env vars compose substitutes at build/run time,
    stage the per-launch bind-mounts (base set + per-instance workspace/
    state — the bundled-skills mount rides along in DOCKER_BASE_MOUNTS),
    load the per-agent conf, and stage the optional-creds bind-mounts
    (with the auto-readme touch). Per-workspace skills aren't mounted —
    Claude Code auto-discovers those from the workspace's `.claude/skills/`
    directory natively. Returns (conf, cred_names) — mounts have all been
    staged via docker_config.add_docker_mount and don't need to flow
    through this return."""
    install_latest_md(sess_id)
    ensure_shared_oauth_files()
    set_container_env(sess_id)
    set_container_mounts(sess_id)
    _, conf = load_conf(sess_id.md_path)
    plant_user_extras(sess_id.modes)
    cred_names = optional_creds_mounts()
    return conf, cred_names


def launch() -> None:
    """Seven-stage orchestrator: input → filesystem validation → resume detection
    → persist → categorise (modes/chain) → setup (state/env/mounts) → run. One
    call per stage so a future operation slots in at the right point with
    localised changes. Whether the launch is new vs continuing is carried on
    the identity itself (inst_id.is_brand_new), not threaded as a separate arg.
    `--dry-run` short-circuits the final stage — all state setup still happens
    so test runs can inspect side effects, but the container build + run is
    skipped."""
    picked, claude_args, dry_run = select_pick()
    if not dry_run:
        require_docker()   # state setup doesn't need docker; only the final run_compose does
    inst_id = resolve_target(picked)
    resume_flag = compute_resume_flag(inst_id)
    update_workspace_map(inst_id)
    sess_id, chain = compose_runtime(inst_id)
    conf, cred_names = setup_state(sess_id)
    print_launch_banner(sess_id, cred_names)
    if dry_run:
        print("  (--dry-run: state setup complete; skipping docker compose run.)")
        return
    run_compose(chain, sess_id.instance, claude_args, resume_flag, conf)


if __name__ == "__main__":
    launch()
