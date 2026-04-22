#!/usr/bin/env python3
import os, sys, subprocess, shutil, json, re, time
from datetime import date
from pathlib import Path
from pick import pick  # pip install pick
from dotenv import dotenv_values  # pip install python-dotenv

PROJECT = Path(__file__).resolve().parent
AGENTS_DIR = PROJECT / "agents"

DEFAULT_CONF = AGENTS_DIR / "default.conf"
MD_EXT = ".md"
CONF_EXT = ".conf"
COMPOSE_FILE = PROJECT / "docker-compose.yml"
AGENTS_STATE = Path.home() / ".claude-agents"
ACCOUNT_FILE = AGENTS_STATE / ".claude.json"
CREDENTIALS_FILE = AGENTS_STATE / ".credentials.json"
AGENT_WORKSPACE_MAP_FILE = AGENTS_STATE / "agent_workspace_map.txt"
DEFAULT_WORKSPACE = os.environ.get("AI_WORKSPACE", "/ai_workspace")

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

SESSION_SEP = "__"
instance_name = lambda agent, session: f"{agent_name_from_stem(agent)}{SESSION_SEP}{session}"
state_dir = lambda agent, session: AGENTS_STATE / instance_name(agent, session)
state_md = lambda agent, session: state_dir(agent, session) / "CLAUDE.md"


def list_sessions(agent):
    """Return session suffixes for existing {agent}__* state dirs, sorted."""
    prefix = f"{agent}{SESSION_SEP}"
    if not AGENTS_STATE.exists():
        return []
    return sorted(
        d.name[len(prefix):] for d in AGENTS_STATE.iterdir()
        if d.is_dir() and d.name.startswith(prefix)
    )


def list_all_instances():
    """Return every `{agent}__{session}` dir under AGENTS_STATE, sorted."""
    if not AGENTS_STATE.exists():
        return []
    return sorted(
        d.name for d in AGENTS_STATE.iterdir()
        if d.is_dir() and SESSION_SEP in d.name
    )


def load_workspace_map():
    """Parse agent_workspace_map.txt as a JSON object."""
    if not AGENT_WORKSPACE_MAP_FILE.exists():
        return {}
    content = AGENT_WORKSPACE_MAP_FILE.read_text().strip()
    return json.loads(content) if content else {}


def save_workspace_map(mapping):
    AGENTS_STATE.mkdir(parents=True, exist_ok=True)
    AGENT_WORKSPACE_MAP_FILE.write_text(json.dumps(mapping, indent=4, sort_keys=True) + "\n")


def _parse_stem(stem):
    """'name(parent)' → ('name', 'parent'); 'name' → ('name', None)."""
    m = re.match(r"^(.+?)\(([^)]+)\)$", stem)
    return (m.group(1), m.group(2)) if m else (stem, None)


def agent_name_from_stem(stem):
    """Strip any '(conf_parent)' suffix — used for display and internal agent ID."""
    return _parse_stem(stem)[0]


def find_md_for_agent(agent_name):
    """Locate an agent's .md by its clean name; handles both '<name>.md' and '<name>(*).md'."""
    direct = AGENTS_DIR / f"{agent_name}{MD_EXT}"
    if direct.exists():
        return direct
    for p in AGENTS_DIR.glob(f"{agent_name}(*){MD_EXT}"):
        return p
    return None


def load_conf(md_path):
    """Locate and load an agent's .conf. Returns (path_or_None, values_dict).
    '<name>(parent).md' aliases to '<parent>.conf'; else '<name>.conf'; falls back to DEFAULT_CONF."""
    name, parent = _parse_stem(md_path.stem)
    specific = AGENTS_DIR / f"{(parent or name)}{CONF_EXT}"
    conf_path = specific if specific.exists() else (DEFAULT_CONF if DEFAULT_CONF.exists() else None)
    return conf_path, (dotenv_values(conf_path) if conf_path else {})


MODEL_FAMILY_RANK = {"opus": 3, "sonnet": 2, "haiku": 1}


def agent_sort_key(item):
    """Sort by family (Opus>Sonnet>Haiku), then version desc, then name asc."""
    name, path = item
    _, conf = load_conf(path)
    model = conf.get("ANTHROPIC_MODEL", "")
    m = re.search(r"(opus|sonnet|haiku)-(\d+)(?:-(\d+))?", model)
    if not m:
        return (0, (0, 0), name)
    return (-MODEL_FAMILY_RANK[m.group(1)], (-int(m.group(2)), -int(m.group(3) or 0)), name)


def instance_sort_key(instance):
    """Reuse agent_sort_key on the agent half, then sub-sort by session; orphans last."""
    agent, _, session = instance.partition(SESSION_SEP)
    md_path = find_md_for_agent(agent)
    if md_path is None:
        return ((1, (0, 0), agent), session)
    return (agent_sort_key((agent, md_path)), session)


def select_agent():
    """Combined picker: new-agent rows, continue-instance rows, and a delete submenu.
    Returns (agent, md_path, session_or_None, workspace_or_None)."""
    MARKER_NEW = "✨ Create"
    MARKER_CONT= " 🏷️ Cont."
    MARKER_DEL = "⚠️ DELETE‼️"
    while True:
        agents = tuple(sorted(
            ((agent_name_from_stem(p.stem), p) for p in AGENTS_DIR.glob(f"*{MD_EXT}") if p.stem != "default"),
            key=agent_sort_key,
        ))
        if not agents:
            sys.exit(f"No agents found. Create an .md file in {AGENTS_DIR}/.")

        width = max(len(name) for name, _ in agents)
        mapping = load_workspace_map()
        instance_width = max(
            (len(instance_name(name, s)) for name, _ in agents for s in list_sessions(name)),
            default=0,
        )

        entries = []
        for name, path in agents:
            desc = path.read_text().splitlines()[0].lstrip("# ").strip()
            entries.append((
                f"{MARKER_NEW}  {name:<{width}} — {desc}",
                ("new", name, path, None, None),
            ))
            for session in list_sessions(name):
                instance = instance_name(name, session)
                workspace = mapping.get(instance)
                ws_display = workspace if workspace and Path(workspace).is_dir() else "?"
                entries.append((
                    f"{MARKER_CONT}      {instance:<{instance_width}}  ( {ws_display} )",
                    ("cont", name, path, session, workspace),
                ))

        entries.append((
            f"{MARKER_DEL}  (Move onto deletions menu)",
            ("delete",),
        ))

        _, idx = pick([e[0] for e in entries], "Select an agent:", indicator="→")
        action = entries[idx][1]

        if action[0] == "delete":
            delete_menu()
            continue
        _, name, path, session, workspace = action
        return name, path, session, workspace


def delete_menu():
    """Flat picker over every `{agent}__{session}` dir. Confirms each deletion;
    stays on this screen until the user picks Back."""
    MARKER_DLET = "🗑 DELETE"
    MARKER_BACK = "🚪  Back"
    while True:
        instances = sorted(list_all_instances(), key=instance_sort_key)
        entries = [(f"{MARKER_DLET}  {i}", i) for i in instances]
        entries.append((f"{MARKER_BACK}  (Move back to Agent Selection)", None))

        _, idx = pick(
            [e[0] for e in entries], "‼️ DELETE AGENT INSTANCES ‼️", indicator="→"
        )
        target = entries[idx][1]

        if target is None:
            return
        if input(f"Deleting '{target}' — Are you sure? [y/N]: ").strip().lower() != "y":
            continue
        shutil.rmtree(AGENTS_STATE / target)
        mapping = load_workspace_map()
        if target in mapping:
            del mapping[target]
            save_workspace_map(mapping)


def prompt_workspace(agent):
    """Prompt for a workspace path; Enter uses DEFAULT_WORKSPACE. Returns resolved absolute path."""
    while True:
        entered = input(
            f"Workspace path for new '{agent}' instance [{DEFAULT_WORKSPACE}]: "
        ).strip() or DEFAULT_WORKSPACE
        resolved = str(Path(entered).expanduser().resolve())
        if Path(resolved).is_dir():
            return resolved
        print(f"Not a directory: {resolved}")


def prompt_session(agent, workspace):
    """Prompt for a session suffix; default = last segment of the workspace path.
    Rejects collisions with existing `{agent}__{suffix}` state dirs."""
    default = Path(workspace).name
    while True:
        suffix = input(f"Session suffix for '{agent}' [{default}]: ").strip() or default
        if not suffix:
            print("Session suffix cannot be empty.")
            continue
        if state_dir(agent, suffix).exists():
            print(f"Instance '{instance_name(agent, suffix)}' already exists. Pick another name.")
            continue
        return suffix


def sync_state(agent, session, md_path):
    """Copy the agent .md as CLAUDE.md into the persistent state dir."""
    sd = state_dir(agent, session)
    (sd / "projects" / "-workspace" / "memory").mkdir(parents=True, exist_ok=True)
    state_md(agent, session).write_text(md_path.read_text())
    if not ACCOUNT_FILE.exists():
        ACCOUNT_FILE.write_text("{}")
    if not CREDENTIALS_FILE.exists():
        CREDENTIALS_FILE.write_text("{}")
    return sd


def prepare_caches():
    """Pre-create shared cache dirs so Docker doesn't auto-create them as root, then prune any
    that have grown past threshold."""
    for host in CACHE_MOUNTS:
        host.mkdir(parents=True, exist_ok=True)
    
    # prune_caches - For each cache over CACHE_PRUNE_THRESHOLD_GB, remove files older than
    # CACHE_PRUNE_MIN_AGE_DAYS. Skipped when any agent container is running (to avoid yanking caches mid-build)
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


def launch():
    """Pick an instance (agent+session), resolve workspace, sync state, exec docker compose."""
    agent, md_path, session, workspace = select_agent()
    resume_flag = ["--continue"] if session is not None else []

    if workspace is None:
        workspace = prompt_workspace(agent)         # pick workspace location
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

    # refresh frequency for Dockerfile's downloaded software
    os.environ["TOOLCHAIN_REFRESH"] = date.today().strftime("%Y-W%W")  # WEEKLY
    os.environ["AGENT_STATE"] = str(sync_state(agent, session, md_path))
    os.environ["AGENT_NAME"] = agent
    pretty = instance.replace("-", " ").replace("__", " - ").title()
    os.environ["AGENT_STATUS_LINE"] = f"\033[36m● {pretty} \033[90m( {workspace} )\033[0m"
    os.environ["AI_WORKSPACE"] = workspace
    os.environ["ACCOUNT_FILE"] = str(ACCOUNT_FILE)
    os.environ["CREDENTIALS_FILE"] = str(CREDENTIALS_FILE)
    prepare_caches()
    conf_path, conf = load_conf(md_path)
    os.environ.update(conf)
    print(f"  Agent definition: {md_path.relative_to(PROJECT)}")
    print(f"  Configuration:    {conf_path.relative_to(PROJECT) if conf_path else '(none — using defaults)'}")
    ensure_image()
    print(f"\033]0;Claude Code — {instance}\007", end="", flush=True)
    cmd = (
        ["docker", "compose", "-f", str(COMPOSE_FILE), "run", "--rm", "-it"]
        + [arg for host, container in CACHE_MOUNTS.items() for arg in ("-v", f"{host}:{container}")]  # -v flags mounting shared toolchain caches (optimization; see CACHE_MOUNTS)
        + [item for key in conf for item in ("-e", key)]   # -e flags forwarding each per-agent conf key as an env var into the container
        + ["claude-code"]
        + resume_flag  # present if a resumed session
        + sys.argv[1:]
    )
    sys.exit(subprocess.call(cmd))


if __name__ == "__main__":
    launch()
