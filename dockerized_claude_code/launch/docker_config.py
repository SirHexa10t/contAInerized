"""Docker-side launcher orchestration — everything between "we picked an agent"
and `docker compose run`. The image-build chain (ensure_image), the env-var
setup compose reads at substitution time (set_container_env), and the launch
banner that summarises what's about to run (print_launch_banner).

Imports from agent_composition (chain helpers + PROJECT), agents_crud (paths
the env exposes), and user_additions (optional-creds INSTALL_* flags). Nothing
else in launch/ imports this module — run.py is the only consumer.
"""

import os
import shutil
import subprocess
import sys
from datetime import date
from pathlib import Path

from .agent_composition import PROJECT, chain_compose_files, chain_image_tag
from .agents_crud import ACCOUNT_FILE, CREDENTIALS_FILE
from .user_additions import FIREWALL_WHITELIST_FILE, optional_creds_install_env


# Weekly cache buster for the Dockerfile's curl-piped downloads (uv, rich-cli,
# Claude Code in `base`; rustup in `prog`). The value flips every Monday, so the
# next build after a week-roll re-fetches those binaries; intra-week builds reuse
# the cache. Bump this manually (e.g. to "force-2026-05-08") to force a refresh
# mid-week without `--no-cache`.
SOFTWARE_STACK_REFRESH = date.today().strftime("%Y-W%W")


def require_docker():
    """Exit early with a clean message if `docker` isn't on PATH. Run.py calls this
    at startup so a missing daemon surfaces as a one-liner instead of a deeper-down
    docker-compose traceback later."""
    if shutil.which("docker") is None:
        sys.exit("docker is required but was not found in PATH.")


def conf_env_args(conf):
    """Convert a per-agent `.conf` dict (from agent_composition.load_conf) into a
    list of `-e KEY=VALUE` args for `docker compose run`. Each conf entry becomes
    a runtime env var inside the container."""
    return [item for k, v in conf.items() for item in ("-e", f"{k}={v}")]


def ensure_image(chain):
    """Build each step in the chain sequentially. Each step's image is tagged
    according to chain_image_tag(chain[:i+1]); PARENT_IMAGE for non-base steps
    points to the prior step's tag so each Dockerfile's `FROM ${PARENT_IMAGE}`
    resolves to a freshly-built parent. Each build invocation uses only
    compose.yml + the step's own compose file (intermediates aren't included
    so their build-args don't surface in unrelated Dockerfile builds)."""
    prev_tag = None
    for i, step in enumerate(chain):
        target = chain_image_tag(chain[:i + 1])
        compose_files = chain_compose_files(["base"] if step == "base" else ["base", step])
        env = {**os.environ, "TARGET_IMAGE": target}
        if prev_tag:
            env["PARENT_IMAGE"] = prev_tag
        print(f"  Building {step} → {target}...")
        ret = subprocess.call(["docker", "compose"] + compose_files + ["build"], env=env)
        if ret != 0:
            sys.exit(ret)
        prev_tag = target


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


def print_launch_banner(md_path, conf_path, tags, modes, skill_mounts, cred_names, whitelist_count):
    """Print the multi-line summary that appears before docker compose builds the
    image — agent definition path, conf path, active tags + modes, and skills/creds
    counts when applicable. Each line is conditional on having something to show
    (no empty 'Tags: ' if there are none). `whitelist_count` is None for non-{auto}
    launches (line hidden); an integer count (possibly 0) when {auto} is active so
    the user sees the file's existence and current size."""
    print(f"  Agent definition: {md_path.relative_to(PROJECT)}")
    print(f"  Configuration:    {conf_path.relative_to(PROJECT) if conf_path else '(none — using defaults)'}")
    if tags:
        print(f"  Tags:             {' '.join(f'[{t}]' for t in tags)}")
    if modes:
        print(f"  Modes:            {' '.join('{' + m + '}' for m in modes)}")
    if skill_mounts:
        print(f"  Project skills:   {len(skill_mounts) // 2} loaded (custom_skills/ + .skills/ if present)")
    if cred_names:
        print(f"  Optional creds:   {', '.join(cred_names)} (from optional_creds/)")
    if whitelist_count is not None:
        plural = "" if whitelist_count == 1 else "s"
        display_path = "~/" + str(FIREWALL_WHITELIST_FILE.relative_to(Path.home()))
        print(f"  User whitelist:   {whitelist_count} domain{plural} (from {display_path})")


def run_compose(chain, instance, claude_args, resume_flag, volume_args, skill_mounts, cred_mounts, conf):
    """Build each image in the chain, set TARGET_IMAGE so compose's `image:`
    substitutes to the chain output, set the terminal title, then exec
    `docker compose run`. sys.exits with the container's return code."""
    ensure_image(chain)
    os.environ["TARGET_IMAGE"] = chain_image_tag(chain)
    compose_args = chain_compose_files(chain)
    print(f"\033]0;Claude Code — {instance}\007", end="", flush=True)
    cmd = (
        ["docker", "compose"] + compose_args + ["run", "--rm", "-it"]
        + volume_args             # tag- and mode-contributed mounts ([prog] caches, etc.; DooD socket / auto firewall live in their compose layers)
        + skill_mounts            # -v flags surfacing each skill from custom_skills/ + workspace's .skills/
        + cred_mounts             # -v flags surfacing each recognized service in optional_creds/
        + conf_env_args(conf)     # -e flags setting each per-agent conf key=value in the container
        + ["claude-code"]
        + resume_flag             # present if a resumed session
        + claude_args             # leftover argv (unrecognised flags + unresolved positional) → claude
    )
    sys.exit(subprocess.call(cmd))
