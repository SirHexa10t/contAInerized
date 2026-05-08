#!/usr/bin/env python3
import os, shutil, subprocess, sys
from datetime import date
from pathlib import Path

from launch.agent_composition import (
    PROJECT, AGENTS_DIR, apply_modes, apply_tags, find_md_for_agent, load_conf, parse_stem,
)
from launch.agents_crud import (
    AGENTS_STATE, AGENT_WORKSPACE_MAP_FILE, ACCOUNT_FILE, CREDENTIALS_FILE,
    SESSION_SEP, instance_name, load_workspace_map, save_workspace_map,
    get_instance_modes, set_instance_modes, install_latest_md, creatable_agents,
)
from launch.menu_picker import select_agent, ask_for_workspace, prompt_dood, prompt_session
from launch.user_additions import (
    aggregated_skills_mounts, ensure_optional_creds_readme,
    optional_creds_install_env, optional_creds_mounts,
)

MODE_DOOD = "DooD"  # mirrors the key in agent_composition.MODE_HANDLERS

if shutil.which("docker") is None:
    sys.exit("docker is required but was not found in PATH.")

COMPOSE_FILE = PROJECT / "docker" / "compose.yml"

# Weekly cache buster for the Dockerfile's curl-piped downloads (uv, rich-cli,
# Claude Code in `base`; rustup in `prog`). The value flips every Monday, so the
# next build after a week-roll re-fetches those binaries; intra-week builds reuse
# the cache. Bump this manually (e.g. to "force-2026-05-08") to force a refresh
# mid-week without `--no-cache`.
SOFTWARE_STACK_REFRESH = date.today().strftime("%Y-W%W")


def has_continuable_history(state_path):
    """Whether the agent state has any actual conversation transcript that
    `claude --continue` can load. Claude Code writes input events to
    `projects/<encoded-workspace>/history.jsonl` (used by the picker for the
    'Last used' timestamp) regardless of whether a conversation actually
    occurred — but the conversation itself lives in a session-UUID JSONL file
    alongside it. If the only thing on disk is `history.jsonl` (or all other
    JSONLs are 0-byte), `--continue` will fail with 'No conversation found
    to continue' and exit. This check lets the launcher fall back to a fresh
    session in that case instead of crashing."""
    projects_dir = state_path / "projects"
    if not projects_dir.is_dir():
        return False
    for jsonl in projects_dir.rglob("*.jsonl"):
        if jsonl.name == "history.jsonl":
            continue
        try:
            if jsonl.stat().st_size > 0:
                return True
        except OSError:
            continue
    return False


def ensure_image(overlays):
    """Build the image stack incrementally. Always builds the base image; for each
    overlay in order, builds with the cumulative `-f` chain so overlay Dockerfiles
    that do `FROM claude-agents:<previous>` find the parent tag already populated.
    `overlays` is a list of compose-override Paths in build order (e.g.
    [compose.prog.yml, compose.dood.yml] for a DooD-tagged-prog agent)."""
    base_args = ["-f", str(COMPOSE_FILE)]
    print("  Building base image...")
    ret = subprocess.call(["docker", "compose"] + base_args + ["build"])
    if ret != 0:
        sys.exit(ret)
    cumulative = list(base_args)
    for overlay in overlays:
        cumulative += ["-f", str(overlay)]
        print(f"  Building overlay: {overlay.name}...")
        ret = subprocess.call(["docker", "compose"] + cumulative + ["build"])
        if ret != 0:
            sys.exit(ret)


def set_container_env(agent, session, workspace, state_path):
    """Populate os.environ with everything the container needs (besides the per-agent conf dict).
    INSTALL_<TOOL> flags from optional_creds_install_env reflect which optional_creds/<name>/
    entries exist; Dockerfile.prog gates each CLI install on the matching flag."""
    pretty = f"{agent.replace('-', ' ').title()} - {session.replace('-', ' ').title()}"
    os.environ.update({
        "SOFTWARE_STACK_REFRESH": SOFTWARE_STACK_REFRESH,
        "AGENT_STATE": str(state_path),
        "AGENT_NAME": agent,
        "AGENT_STATUS_LINE": f"\033[36m● {pretty} \033[90m( {workspace} )\033[0m",
        "AI_WORKSPACE": workspace,
        "ACCOUNT_FILE": str(ACCOUNT_FILE),
        "CREDENTIALS_FILE": str(CREDENTIALS_FILE),
    })
    os.environ.update(optional_creds_install_env())


def parse_target():
    """If sys.argv[1] names an existing instance ('agent__session') or a known agent, consume
    it and return a (kind, payload) tuple shaped like select_agent's return. Otherwise None
    (the picker will run, and any args fall through to `claude`)."""
    if len(sys.argv) < 2 or sys.argv[1].startswith("-"):
        return None
    target = sys.argv[1]

    if SESSION_SEP in target and (AGENTS_STATE / target).is_dir():
        agent, _, session = target.partition(SESSION_SEP)
        md_path = find_md_for_agent(agent)
        if md_path is not None:
            sys.argv.pop(1)
            return ("cont", {
                "agent_name": agent,
                "md_path": md_path,
                "session": session,
                "workspace": load_workspace_map().get(target),
            })

    md_path = find_md_for_agent(target)
    if md_path is not None:
        sys.argv.pop(1)
        return ("new", {"agent_name": target, "md_path": md_path})

    return None


def launch():
    """Pick an instance (agent+session), resolve workspace, sync state, exec docker compose."""
    if not creatable_agents():
        sys.exit(f"No agents found. Create an .md file in {AGENTS_DIR}/.")
    pick = parse_target() or select_agent()
    if pick is None:
        sys.exit(0)

    kind, payload = pick
    agent = payload["agent_name"]
    md_path = payload["md_path"]
    if kind == "new":
        session, workspace = None, None
    else:  # "cont"
        session = payload["session"]
        workspace = payload["workspace"]

    if session is not None and has_continuable_history(AGENTS_STATE / instance_name(agent, session)):
        resume_flag = ["--continue"]
    elif session is not None:
        # "Cont." was picked but the instance has no actual conversation transcript
        # yet (e.g. a previous launch was quit immediately, leaving only the input
        # log in history.jsonl). Skip --continue so Claude Code starts a fresh
        # session in this instance instead of crashing on "No conversation found".
        print(f"  (Instance '{instance_name(agent, session)}' has no prior conversation; starting fresh.)")
        resume_flag = []
    else:
        resume_flag = []

    if workspace is None:
        workspace = ask_for_workspace(agent)        # pick workspace location
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

    tags = parse_stem(md_path.stem)[1]
    # Mode resolution — new [prog] instances prompt for DooD; cont reuses what's stored.
    # Non-prog agents skip the prompt (their image has no docker CLI for DooD to drive).
    if kind == "new" and "prog" in tags:
        modes = [MODE_DOOD] if prompt_dood() else []
        set_instance_modes(instance, modes)
    else:
        modes = get_instance_modes(instance)

    try:
        tag_extras = apply_tags(tags)
        mode_extras = apply_modes(modes)
    except (ValueError, RuntimeError) as e:
        sys.exit(f"  {e}")
    # Tag overlays first, then mode overlays — DooD's Dockerfile.dood does
    # `FROM claude-agents:prog`, so the prog overlay must layer in earlier.
    overlays = tag_extras["compose_overrides"] + mode_extras["compose_overrides"]
    volume_args = tag_extras["volume_args"] + mode_extras["volume_args"]
    compose_args = ["-f", str(COMPOSE_FILE)] + [arg for p in overlays for arg in ("-f", str(p))]

    state_path = install_latest_md(agent, session, md_path)
    set_container_env(agent, session, workspace, state_path)
    conf_path, conf = load_conf(md_path)
    print(f"  Agent definition: {md_path.relative_to(PROJECT)}")
    print(f"  Configuration:    {conf_path.relative_to(PROJECT) if conf_path else '(none — using defaults)'}")
    if tags:
        print(f"  Tags:             {' '.join(f'[{t}]' for t in tags)}")
    if modes:
        print(f"  Modes:            {' '.join('{' + m + '}' for m in modes)}")
    skill_mounts = aggregated_skills_mounts(workspace, state_path)
    if skill_mounts:
        print(f"  Project skills:   {len(skill_mounts) // 2} loaded (custom_skills/ + .skills/ if present)")
    ensure_optional_creds_readme()
    cred_mounts, cred_names = optional_creds_mounts()
    if cred_names:
        print(f"  Optional creds:   {', '.join(cred_names)} (from optional_creds/)")
    ensure_image(overlays)
    print(f"\033]0;Claude Code — {instance}\007", end="", flush=True)
    cmd = (
        ["docker", "compose"] + compose_args + ["run", "--rm", "-it"]
        + volume_args             # tag- and mode-contributed mounts ([prog] caches, DooD socket already in compose.dood.yml, etc.)
        + skill_mounts            # -v flags surfacing each skill from custom_skills/ + workspace's .skills/
        + cred_mounts             # -v flags surfacing each recognized service in optional_creds/
        + [item for k, v in conf.items() for item in ("-e", f"{k}={v}")]  # -e flags setting each per-agent conf key=value in the container
        + ["claude-code"]
        + resume_flag             # present if a resumed session
        + sys.argv[1:]
    )
    sys.exit(subprocess.call(cmd))


if __name__ == "__main__":
    launch()
