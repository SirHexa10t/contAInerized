#!/usr/bin/env python3
import argparse
import dataclasses
import sys
from typing import NamedTuple

from launch.agents_crud import (
    install_latest_md, install_settings, persist_instance, resolve_pick,
)
from launch.container_env import set_container_env
from launch.docker_config import (
    ensure_image, prompt_install_failures, require_docker, run_container,
    set_container_mounts, set_dry_run,
)
from launch.file_access import agent_md_index, ensure_shared_oauth_files, is_dir
from launch.menu_picker import (
    ask_for_workspace, print_launch_banner, prompt_session, prompt_tags, select_agent,
)
from launch.paths import AGENTS_DIR, INSTANCES_FILE
from launch.tag_handlers import apply_tags
from launch.tags import (
    Agent, Instance, Registry, TagError, migrations, resolve_build, scan_all,
)
from launch.user_additions import (
    optional_creds_mounts, plant_user_extras,
)
from launch.utils import call_or_exit, exit_if_missing


class LaunchOptions(NamedTuple):
    """One launch's CLI-derived inputs — what parse_cli returns and the later
    stages consume. NamedTuple (not a bare tuple) so call sites read
    `opts.dry_run` instead of positional index 2, and flag N+1 is one new
    field here instead of a wider tuple threaded through every signature.
    Field meanings:
        picked            — Agent (new) | Instance (cont, is_brand_new=False)
                            if a known agent/instance name was given as the
                            positional arg, else None (the picker will run;
                            gather_input fills it in).
        claude_args       — anything else from argv: flags argparse didn't
                            recognize, plus the positional if it didn't resolve
                            to a known target. Appended to the container's
                            `claude-code` command so they reach claude inside.
        dry_run           — `--dry-run` flag. launch() runs every stage but the
                            docker-touching steps no-op (build/run invocations
                            print; install-failure read skips).
        refresh_installs  — `--refresh-installs` flag. Every optional CLI
                            install in the [code] Dockerfile re-runs (cache
                            buster — used to retry previously-failed installs).
                            Already-installed tools fast-path through their
                            package manager's no-op."""
    picked: Agent | Instance | None
    claude_args: list[str]
    dry_run: bool
    refresh_installs: bool


def parse_cli(registry: Registry) -> LaunchOptions:
    """Parse the launcher's CLI into a LaunchOptions (see its docstring for
    field semantics).

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
        help="Run all state setup but skip the final docker run step.",
    )
    parser.add_argument(
        "--refresh-installs",
        action="store_true",
        help="Force-rebuild every optional CLI install in the [code] Dockerfile (busts the "
             "FORCE_INSTALLS_REFRESH and SOFTWARE_STACK_REFRESH layer caches). Used "
             "to retry installs that failed in a prior launch.",
    )
    args, claude_args = parser.parse_known_args()
    picked = resolve_pick(args.target, registry)
    if args.target is not None and picked is None:
        # Unknown name — pass it through to claude as a positional, picker still runs.
        claude_args = [args.target] + claude_args
    return LaunchOptions(picked, claude_args, args.dry_run, args.refresh_installs)


def gather_input() -> tuple[LaunchOptions, Registry]:
    """Stage 1 — Input. Scan the tag tree (fail loud on a malformed tree —
    nothing downstream can run without the taxonomy), run any pending
    store-format migration, verify there are agents to pick from, parse CLI
    args, gate on docker being installed, fall back to the interactive picker
    if no target was given on the command line, exit cleanly if the user
    cancels. Returns (LaunchOptions with `picked` guaranteed non-None,
    registry) — an Agent for new, an Instance for cont (the new/cont
    distinction is encoded in the type).

    The docker gate sits between parse_cli and the picker deliberately:
    after parse_cli so `--help` still works on a docker-less machine, but
    before select_agent so the user isn't walked through the picker and
    prompts for a launch that was never going to happen. It fires in dry-run
    too — dry-run is a faithful projection of a real run."""
    registry = call_or_exit(scan_all, AGENTS_DIR, exceptions=TagError)
    migrations.ensure_migrated()
    exit_if_missing(agent_md_index(), f"No agents found. Create an .md file in {AGENTS_DIR}/.")
    opts = parse_cli(registry)
    require_docker()
    picked = opts.picked or select_agent(registry)
    if picked is None:
        sys.exit(0)
    return opts._replace(picked=picked), registry


def resolve_target(picked: Agent | Instance, registry: Registry) -> Instance:
    """Stage 2 — Filesystem validation + identity completion. For cont,
    `picked` is already a full Instance (stored workspace + resolved tags +
    is_brand_new=False baked in by the picker/resolve_pick), so just validate
    the workspace and pass it through (re-prompting if the store entry was
    missing). For new, `picked` is an Agent — prompt workspace + session +
    tags here (the tag form pre-checked from its `.lego` defaults) so the
    returned Instance is fully resolved before downstream stages run.
    is_brand_new=True is stamped on at the same time. Esc on the tag form
    cancels the whole launch (clean exit — nothing has been persisted at
    that point)."""
    if isinstance(picked, Instance):       # cont — workspace + tags + is_brand_new already set
        if picked.workspace and not is_dir(picked.workspace):
            sys.exit(
                f"  Workspace for '{picked.instance}' is not a directory: {picked.workspace}\n"
                f"  Repoint it via the picker's F2 modify, or edit {INSTANCES_FILE}."
            )
        if not picked.workspace:           # stale / missing store entry (None or "") — re-prompt
            return dataclasses.replace(picked, workspace=ask_for_workspace(picked.agent))
        return picked
    # new — Agent only; prompt workspace, session, then tags
    workspace = ask_for_workspace(picked.name)
    session = prompt_session(picked.name, workspace)
    build = prompt_tags(registry, picked.build)
    if build is None:
        sys.exit(0)
    return Instance(agent=picked.name, md_path=picked.md_path, session=session,
                    workspace=workspace, is_brand_new=True,
                    **resolve_build(build, picked.name, registry))


def compute_resume_flag(inst: Instance) -> list[str]:
    """Stage 3 — Resume detection. Returns the claude args needed to resume an
    existing conversation (`["--continue"]`) or `[]` for a fresh session. Cont
    with no transcript prints a notice and starts fresh — `--continue` against
    history-only state crashes claude with 'No conversation found'."""
    if inst.is_brand_new:
        return []
    if inst.has_continuable_history:
        return ["--continue"]
    print(f"  (Instance '{inst.instance}' has no prior conversation; starting fresh.)")
    return []


def setup_state(inst: Instance, refresh_installs: bool = False) -> list[str]:
    """Stage 6 — Setup. Install the agent's `.md` plus the active-chain
    addendum section into its state dir as CLAUDE.md (a single overwrite —
    install_latest_md keys off inst.chain for the addendums), ensure
    shared OAuth state files exist so docker doesn't auto-create them as
    root, populate the env vars the container build/run substitutes,
    stage the per-launch bind-mounts (base set + per-instance workspace/
    state — the bundled-skills mount rides along in DOCKER_BASE_MOUNTS),
    and stage the optional-creds bind-mounts (with the auto-readme touch).
    Per-workspace skills aren't mounted — Claude Code auto-discovers those
    from the workspace's `.claude/skills/` directory natively. Returns
    cred_names — mounts have all been staged via
    docker_config.add_docker_mount, and the engine conf rides on the
    Instance itself (`inst.conf`), so neither flows through this return.

    `refresh_installs` propagates to set_container_env, which busts both
    refresh-cache-buster ARGs so every optional CLI install retries on the
    upcoming build."""
    install_latest_md(inst)
    # Policy-conflict TagError → clean exit naming both culprit policies.
    call_or_exit(install_settings, inst, exceptions=TagError)
    ensure_shared_oauth_files()
    set_container_env(inst, refresh_installs=refresh_installs)
    set_container_mounts(inst)
    plant_user_extras(inst)
    # optional_creds_mounts may raise RuntimeError on a clash from a contents-
    # mount entry (e.g. `home/.bashrc` shadowing the bundled bashrc mount);
    # call_or_exit surfaces it as a clean sys.exit with the helpful message.
    return call_or_exit(optional_creds_mounts, exceptions=RuntimeError)


def launch() -> None:
    """Seven-stage orchestrator: input → filesystem validation → resume detection
    → persist → categorise (chain) → setup (state/env/mounts) → run. One call
    per stage so a future operation slots in at the right point with localised
    changes. Whether the launch is new vs continuing is carried on the identity
    itself (inst.is_brand_new), not threaded as a separate arg. `--dry-run`
    propagates into docker_config via set_dry_run; the only behavioural
    differences are that docker_subprocess prints its would-be
    invocation instead of running it and prompt_install_failures skips its
    image read (nothing was built, so any log it found would be stale) —
    every other step (mount staging, env staging, firewall coordination,
    banner) runs identically so dry-run projects what a real run would do
    (including failing fast, inside gather_input, if docker isn't on PATH).
    `--refresh-installs` busts the install layer caches via set_container_env."""
    opts, registry = gather_input()
    assert opts.picked is not None   # gather_input exits on picker cancel — narrows for the type checker
    set_dry_run(opts.dry_run)
    inst = resolve_target(opts.picked, registry)
    resume_flag = compute_resume_flag(inst)
    # Persist BOTH new and cont launches — the store entry always reflects the
    # last-launched configuration (idempotent rewrite on unchanged cont).
    persist_instance(inst)
    # apply_tags is side-effect-driven (tag.docker mounts, cache prep, GID
    # staging, the {firewall} DNS kickoff); the chain it returns already
    # rides the Instance for everything downstream.
    call_or_exit(apply_tags, inst, exceptions=(ValueError, RuntimeError))
    cred_names = setup_state(inst, refresh_installs=opts.refresh_installs)
    print_launch_banner(inst, cred_names)
    # Build the image stack here (not inside run_container) so the next
    # step can read the just-built image's failure log before Claude Code's
    # TUI takes over.
    image = ensure_image(inst)
    # Surfaces any failed-install names + retry hint before run_container execs into Claude Code's TUI.
    prompt_install_failures(image, inst.instance)
    run_container(inst, image, opts.claude_args, resume_flag)


if __name__ == "__main__":
    launch()
