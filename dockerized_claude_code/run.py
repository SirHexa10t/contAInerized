#!/usr/bin/env python3
import argparse
import dataclasses
import sys

from launch.agent_modifiers_handler import compose_chain
from launch.agents_crud import (
    install_latest_md, resolve_pick, set_instance_modes,
    update_workspace_map,
)
from launch.compose_env import set_container_env
from launch.docker_config import (
    ensure_image, prompt_install_failures, require_docker, run_compose,
    set_container_mounts, set_dry_run,
)
from launch.file_access import ensure_shared_oauth_files, load_conf
from launch.menu_picker import (
    ask_for_workspace, print_launch_banner, prompt_modes, prompt_session, select_agent,
)
from launch.paths import AGENT_MD_BY_NAME, AGENTS_DIR
from launch.structs import AgentIdentity, InstanceIdentity
from launch.user_additions import (
    optional_creds_mounts, plant_user_extras,
)
from launch.utils import call_or_exit, exit_if_missing


def parse_cli() -> tuple[AgentIdentity | InstanceIdentity | None, list[str], bool, bool]:
    """Parse the launcher's CLI. Returns (picked, claude_args, dry_run, refresh_installs):
        picked            — AgentIdentity (new) | InstanceIdentity (cont, is_brand_new=False)
                            if a known agent/instance name was given as the positional arg,
                            else None (picker will run).
        claude_args       — anything else from argv: flags argparse didn't recognize, plus
                            the positional if it didn't resolve to a known target. These
                            get appended to the `docker compose run … claude-code` command
                            so they reach claude inside the container.
        dry_run           — `--dry-run` flag. When True, launch() runs every stage
                            (state setup, mount staging, etc.) but skips the final
                            `docker compose run`.
        refresh_installs  — `--refresh-installs` flag. When True, every optional CLI
                            install in Dockerfile.code re-runs (cache buster — used
                            to retry previously-failed installs). Already-installed
                            tools fast-path through their package manager's no-op.

    Use `--` to force args through to claude even when they look like our own flags
    (e.g. `python3 run.py poet -- --help` runs poet and passes --help to claude).
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
    parser.add_argument(
        "--refresh-installs",
        action="store_true",
        help="Force-rebuild every optional CLI install in Dockerfile.code (busts the "
             "FORCE_INSTALLS_REFRESH and SOFTWARE_STACK_REFRESH layer caches). Used "
             "to retry installs that failed in a prior launch.",
    )
    args, claude_args = parser.parse_known_args()
    picked = resolve_pick(args.target)
    if args.target is not None and picked is None:
        # Unknown name — pass it through to claude as a positional, picker still runs.
        claude_args = [args.target] + claude_args
    return picked, claude_args, args.dry_run, args.refresh_installs


def gather_input() -> tuple[AgentIdentity | InstanceIdentity, list[str], bool, bool]:
    """Stage 1 — Input. Verify there are agents to pick from, parse CLI args, fall
    back to the interactive picker if no target was given on the command line, exit
    cleanly if the user cancels. Returns (picked, claude_args, dry_run,
    refresh_installs) — `picked` is an AgentIdentity for new and an InstanceIdentity
    for cont. The new/cont distinction is encoded in the returned type. `dry_run`
    and `refresh_installs` come straight off the CLI flags — see parse_cli."""
    exit_if_missing(AGENT_MD_BY_NAME, f"No agents found. Create an .md file in {AGENTS_DIR}/.")
    picked, claude_args, dry_run, refresh_installs = parse_cli()
    picked = picked or select_agent()
    if picked is None:
        sys.exit(0)
    return picked, claude_args, dry_run, refresh_installs


def resolve_target(picked: AgentIdentity | InstanceIdentity) -> InstanceIdentity:
    """Stage 2 — Filesystem validation + identity completion. For cont, `picked`
    is already a full InstanceIdentity (stored workspace + modes +
    is_brand_new=False baked in by the picker), so just validate the workspace
    and pass it through (re-prompting if the map entry was missing). For new,
    `picked` is an AgentIdentity — prompt workspace + session + modes here so
    the returned InstanceIdentity is fully resolved before downstream stages
    run. is_brand_new=True is stamped on at the same time."""
    if isinstance(picked, InstanceIdentity):       # cont — workspace + session + modes + is_brand_new already set
        picked.validate_workspace()
        if picked.workspace is None:               # stale / missing map entry — re-prompt
            return dataclasses.replace(picked, workspace=ask_for_workspace(picked.agent))
        return picked
    # new — AgentIdentity only; prompt workspace, session, then modes
    workspace = ask_for_workspace(picked.agent)
    session = prompt_session(picked.agent, workspace)
    modes = prompt_modes(picked.tags, current_modes=())
    return InstanceIdentity(agent=picked.agent, session=session, workspace=workspace,
                            is_brand_new=True, modes=tuple(modes))


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


def setup_state(inst_id: InstanceIdentity, refresh_installs: bool = False) -> tuple[dict[str, str], list[str]]:
    """Stage 6 — Setup. Install the agent's `.md` plus the active-chain
    addendum section into its state dir as CLAUDE.md (a single overwrite —
    install_latest_md keys off inst_id.chain for the addendums), ensure
    shared OAuth state files exist so docker doesn't auto-create them as
    root, populate the env vars compose substitutes at build/run time,
    stage the per-launch bind-mounts (base set + per-instance workspace/
    state — the bundled-skills mount rides along in DOCKER_BASE_MOUNTS),
    load the per-agent conf, and stage the optional-creds bind-mounts
    (with the auto-readme touch). Per-workspace skills aren't mounted —
    Claude Code auto-discovers those from the workspace's `.claude/skills/`
    directory natively. Returns (conf, cred_names) — mounts have all been
    staged via docker_config.add_docker_mount and don't need to flow
    through this return.

    `refresh_installs` propagates to set_container_env, which busts both
    refresh-cache-buster ARGs so every optional CLI install retries on the
    upcoming build."""
    install_latest_md(inst_id)
    ensure_shared_oauth_files()
    set_container_env(inst_id, refresh_installs=refresh_installs)
    set_container_mounts(inst_id)
    _, conf = load_conf(inst_id.md_path)
    plant_user_extras(inst_id.modes)
    # optional_creds_mounts may raise RuntimeError on a clash from a contents-
    # mount entry (e.g. `home/.bashrc` shadowing the bundled bashrc mount);
    # call_or_exit surfaces it as a clean sys.exit with the helpful message.
    cred_names = call_or_exit(optional_creds_mounts, exceptions=RuntimeError)
    return conf, cred_names


def launch() -> None:
    """Seven-stage orchestrator: input → filesystem validation → resume detection
    → persist → categorise (chain) → setup (state/env/mounts) → run. One call
    per stage so a future operation slots in at the right point with localised
    changes. Whether the launch is new vs continuing is carried on the identity
    itself (inst_id.is_brand_new), not threaded as a separate arg. `--dry-run`
    propagates into docker_config via set_dry_run; the only behavioural
    difference is that docker_compose_subprocess prints its would-be invocation
    instead of running it — every other step (mount staging, env staging,
    firewall coordination, banner) runs identically so dry-run projects what
    a real run would do (including failing fast if docker isn't on PATH).
    `--refresh-installs` busts the install layer caches via set_container_env."""
    picked, claude_args, dry_run, refresh_installs = gather_input()
    set_dry_run(dry_run)
    require_docker()
    inst_id = resolve_target(picked)
    resume_flag = compute_resume_flag(inst_id)
    update_workspace_map(inst_id)
    if inst_id.is_brand_new:
        set_instance_modes(inst_id)   # warns inside if both auto+DooD are set
    chain = call_or_exit(compose_chain, inst_id, exceptions=(ValueError, RuntimeError))
    conf, cred_names = setup_state(inst_id, refresh_installs=refresh_installs)
    print_launch_banner(inst_id, cred_names)
    # Build the chain's images here (not inside run_compose) so the next
    # step can read the just-built image's failure log before Claude Code's
    # TUI takes over.
    ensure_image(chain)
    # Surfaces any failed-install names + retry hint before run_compose execs into Claude Code's TUI.
    prompt_install_failures(chain, inst_id.instance)
    run_compose(chain, inst_id.instance, claude_args, resume_flag, conf)


if __name__ == "__main__":
    launch()
