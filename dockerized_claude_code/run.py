#!/usr/bin/env python3
import os, shutil, subprocess, sys, time
from datetime import date
from pathlib import Path

from launch.agents_lib import (
    PROJECT, AGENTS_DIR, AGENTS_STATE, AGENT_WORKSPACE_MAP_FILE, ACCOUNT_FILE, CREDENTIALS_FILE,
    SESSION_SEP, instance_name, find_md_for_agent, load_conf, load_workspace_map, save_workspace_map,
    install_latest_md, prompt_session, creatable_agents,
)
from launch.menu_picker import select_agent, ask_for_workspace

if shutil.which("docker") is None:
    sys.exit("docker is required but was not found in PATH.")

COMPOSE_FILE = PROJECT / "docker-compose.yml"

CACHE_ROOT = AGENTS_STATE / "cache"
CACHE_HOME_IN_CONTAINER = Path("/home/claude")
CACHE_REL_PATHS = [  # shared across all agents/sessions; same relative path on host and in container
    # languages currently in the image
    ".cargo/registry",           # Rust crates (.crate tarballs + index)
    ".cargo/git",                # Rust git dependencies
    ".cache",                    # XDG cache: uv, pip, poetry, pre-commit, huggingface, torch, yarn-v1, go-build, ccache, ...
    # speculative — empty until the relevant language is added to the Dockerfile
    "go/pkg/mod",                # Go module cache
    ".npm",                      # npm (non-XDG by design)
    ".local/share/pnpm/store",   # pnpm content-addressed store
    ".m2/repository",            # Maven local repository
    ".gradle/caches",            # Gradle dependency + build caches
    ".gem",                      # Ruby gems
    ".cpanm",                    # Perl cpanminus work dir
    ".cpan",                     # Perl CPAN classic
    ".cabal/store",              # Haskell cabal package store
    ".stack/snapshots",          # Haskell stack resolver snapshots
]
CACHE_MOUNTS = {CACHE_ROOT / rel: CACHE_HOME_IN_CONTAINER / rel for rel in CACHE_REL_PATHS}
CACHE_PRUNE_THRESHOLD_GB = 5   # per-cache size at which prune kicks in
CACHE_PRUNE_MIN_AGE_DAYS = 7   # files younger than this are kept even when over threshold


def prepare_caches():
    """Pre-create shared cache dirs so Docker doesn't auto-create them as root."""
    for host in CACHE_MOUNTS:
        host.mkdir(parents=True, exist_ok=True)


def prune_caches():
    """For caches above CACHE_PRUNE_THRESHOLD_GB, remove files older than CACHE_PRUNE_MIN_AGE_DAYS.
    Skipped when any agent container is running (to avoid yanking caches mid-build)."""
    result = subprocess.run(
        ["docker", "ps", "--filter", "name=claude-code_", "--format", "{{.Names}}"],
        capture_output=True, text=True,
    )
    if result.returncode != 0 or result.stdout.strip():
        return
    time_cutoff = time.time() - CACHE_PRUNE_MIN_AGE_DAYS * 86400  # days → seconds (match epoch-second time.time())
    size_cutoff = CACHE_PRUNE_THRESHOLD_GB * 1024**3              # GB   → bytes   (match st_size units)
    for host in CACHE_MOUNTS:
        if not host.exists():
            continue
        files = [(f, f.stat()) for f in host.rglob("*") if f.is_file()]
        total = sum(s.st_size for _, s in files)
        if total <= size_cutoff:
            continue
        freed = 0
        for f, s in files:
            if s.st_mtime < time_cutoff:
                f.unlink()
                freed += s.st_size
        if freed:
            print(f"  Pruned {host.relative_to(CACHE_ROOT)}: freed {freed / 1024**3:.1f} GB (was {total / 1024**3:.1f} GB)")


def ensure_image():
    """Rebuild the image."""
    print("  Building image...")
    ret = subprocess.call(["docker", "compose", "-f", str(COMPOSE_FILE), "build"])
    if ret != 0:
        sys.exit(ret)


def set_container_env(agent, session, workspace, state_path):
    """Populate os.environ with everything the container needs (besides the per-agent conf dict)."""
    pretty = f"{agent.replace('-', ' ').title()} - {session.replace('-', ' ').title()}"
    os.environ.update({
        "TOOLCHAIN_REFRESH": date.today().strftime("%Y-W%W"),  # weekly cache key for Dockerfile downloads
        "AGENT_STATE": str(state_path),
        "AGENT_NAME": agent,
        "AGENT_STATUS_LINE": f"\033[36m● {pretty} \033[90m( {workspace} )\033[0m",
        "AI_WORKSPACE": workspace,
        "ACCOUNT_FILE": str(ACCOUNT_FILE),
        "CREDENTIALS_FILE": str(CREDENTIALS_FILE),
    })


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

    resume_flag = ["--continue"] if session is not None else []

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

    state_path = install_latest_md(agent, session, md_path)
    set_container_env(agent, session, workspace, state_path)
    prepare_caches()
    prune_caches()
    conf_path, conf = load_conf(md_path)
    print(f"  Agent definition: {md_path.relative_to(PROJECT)}")
    print(f"  Configuration:    {conf_path.relative_to(PROJECT) if conf_path else '(none — using defaults)'}")
    ensure_image()
    print(f"\033]0;Claude Code — {instance}\007", end="", flush=True)
    cmd = (
        ["docker", "compose", "-f", str(COMPOSE_FILE), "run", "--rm", "-it"]
        + [arg for host, container in CACHE_MOUNTS.items() for arg in ("-v", f"{host}:{container}")]  # -v flags mounting shared toolchain caches (optimization; see CACHE_MOUNTS)
        + [item for k, v in conf.items() for item in ("-e", f"{k}={v}")]  # -e flags setting each per-agent conf key=value in the container
        + ["claude-code"]
        + resume_flag  # present if a resumed session
        + sys.argv[1:]
    )
    sys.exit(subprocess.call(cmd))


if __name__ == "__main__":
    launch()
