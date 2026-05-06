"""Agent composition layer: parses the agent filename grammar
(`<name>([tag]|(parent))*`), locates the matching `.md`, picks the right `.conf`,
sorts agents by model family/version, and dispatches per-tag handlers (apply_tags)
that contribute compose overrides + bind-mounts to the docker compose run.

Imports nothing from agents_crud, run.py, or menu_picker — all of them import from here.
"""

import os
import re
import subprocess
import time
from pathlib import Path

from dotenv import dotenv_values  # pip install python-dotenv

PROJECT = Path(__file__).resolve().parent.parent  # this file lives in launch/, project root is one up
AGENTS_DIR = PROJECT / "agents"
AGENTS_STATE = Path.home() / ".claude-agents"   # shared with agents_crud (which derives ACCOUNT_FILE etc. from this)

DEFAULT_CONF = AGENTS_DIR / "default.conf"
MD_EXT = ".md"
CONF_EXT = ".conf"
MODEL_FAMILY_RANK = {"opus": 3, "sonnet": 2, "haiku": 1}

# === Shared toolchain caches — mounted only when an agent has the [prog] tag ===
CACHE_ROOT = AGENTS_STATE / "cache"
CACHE_HOME_IN_CONTAINER = Path("/home/claude")
CACHE_REL_PATHS = [  # shared across all [prog] agents/sessions; same relative path on host and in container
    # languages currently in the prog image
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


def parse_stem(stem):
    """Parse a filename stem into (name, tags, parent).

    Grammar: <name>(<bracketed-tag>|<parenthesized-parent>)*
      - `[tag]` accumulates into tags (list, in the order they appear).
      - `(parent)` is single-valued; if repeated, last wins.
      - Order between brackets and parens is free: 'name[prog](thinker)' and
        'name(thinker)[prog]' both parse the same way.

    Examples:
        'name'                → ('name', [], None)
        'name(thinker)'       → ('name', [], 'thinker')
        'name[prog]'          → ('name', ['prog'], None)
        'name[prog](thinker)' → ('name', ['prog'], 'thinker')
        'name[a][b]'          → ('name', ['a', 'b'], None)
    """
    m = re.match(r"^([^()\[\]]+)", stem)
    if not m:
        return (stem, [], None)
    name = m.group(1)
    tags = []
    parent = None
    for paren, bracket in re.findall(r"\(([^()]+)\)|\[([^\[\]]+)\]", stem[len(name):]):
        if paren:
            parent = paren
        else:
            tags.append(bracket)
    return (name, tags, parent)


def find_md_for_agent(agent_name):
    """Locate an agent's .md by its clean name; handles any [tag]/(parent) suffix combination
    in the filename. Glob can't express the new grammar (`[` is a glob metacharacter), so we
    enumerate `.md` files and match on the parsed stem — cheap with the project's agent count."""
    for path in AGENTS_DIR.glob(f"*{MD_EXT}"):
        if parse_stem(path.stem)[0] == agent_name:
            return path
    return None


def load_conf(md_path):
    """Locate and load an agent's .conf. Returns (path_or_None, values_dict).
    A '(parent)' suffix in the filename aliases to '<parent>.conf'; otherwise '<name>.conf';
    falls back to DEFAULT_CONF. Tags are ignored here — handled by apply_tags()."""
    name, _, parent = parse_stem(md_path.stem)
    specific = AGENTS_DIR / f"{(parent or name)}{CONF_EXT}"
    conf_path = specific if specific.exists() else (DEFAULT_CONF if DEFAULT_CONF.exists() else None)
    return conf_path, (dotenv_values(conf_path) if conf_path else {})


def parse_model_id(model):
    """Extract (family, major, minor) from a model ID like 'claude-opus-4-7'.
    Returns None when no recognized family is present."""
    m = re.search(r"(opus|sonnet|haiku)-(\d+)(?:-(\d+))?", model)
    if not m:
        return None
    return m.group(1), int(m.group(2)), int(m.group(3) or 0)


def agent_sort_key(item):
    """Sort by family (Opus>Sonnet>Haiku), then version desc, then name asc."""
    name, path = item
    _, conf = load_conf(path)
    parsed = parse_model_id(conf.get("ANTHROPIC_MODEL", ""))
    if parsed is None:
        return (0, (0, 0), name)
    family, major, minor = parsed
    return (-MODEL_FAMILY_RANK[family], (-major, -minor), name)


# === Tag dispatch: each handler returns the extras its tag contributes to the docker compose run ===

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


def _apply_prog():
    """[prog] tag: switch the build target to `prog` (build-essential, Rust, Node)
    via docker/compose.prog.yml, and mount shared toolchain caches under the
    container user's $HOME so package downloads survive across rebuilds."""
    prepare_caches()
    prune_caches()
    return {
        "compose_overrides": [PROJECT / "docker" / "compose.prog.yml"],
        "volume_args": [arg for h, c in CACHE_MOUNTS.items() for arg in ("-v", f"{h}:{c}")],
    }


TAG_HANDLERS = {
    "prog": _apply_prog,
}


def apply_tags(tags):
    """Run each tag's handler and aggregate their contributions.
    Returns {compose_overrides: [Path,...], volume_args: [str,...]}.
    Unknown tags raise ValueError so a typo in a filename surfaces loudly rather than silently doing nothing."""
    aggregated = {"compose_overrides": [], "volume_args": []}
    for tag in tags:
        if tag not in TAG_HANDLERS:
            raise ValueError(f"Unknown agent tag: [{tag}]. Known tags: {sorted(TAG_HANDLERS)}")
        contribution = TAG_HANDLERS[tag]()
        for key in aggregated:
            aggregated[key].extend(contribution.get(key, []))
    return aggregated


# === Mode dispatch — like tags, but per-instance (set at create/modify time, stored in agent_modes_map.json) ===

def _detect_docker_gid():
    """Return the host's docker group GID as a string, or None if no docker group exists.
    Used as the DOCKER_GID build-arg for Dockerfile.dood (so claude can read/write the
    bind-mounted /var/run/docker.sock)."""
    try:
        result = subprocess.run(
            ["getent", "group", "docker"],
            capture_output=True, text=True, check=False,
        )
    except FileNotFoundError:
        return None  # no getent (e.g., not Linux)
    if result.returncode == 0 and result.stdout.strip():
        return result.stdout.strip().split(":")[2]
    return None


def _apply_dood():
    """[DooD mode]: bind-mount the host's /var/run/docker.sock so the agent can drive
    the host's Docker daemon (run sub-containers, build images, etc.). Builds
    claude-agents:dood (FROM claude-agents:prog + docker-ce-cli + docker-compose-plugin)
    via docker/compose.dood.yml.

    Detects the host's docker-group GID via getent and exports it as DOCKER_GID so
    compose can pass it as a build-arg. Without a docker group on the host, the agent
    couldn't access the bind-mounted socket — so we fail loudly here rather than build
    an image that won't work."""
    gid = _detect_docker_gid()
    if gid is None:
        raise RuntimeError(
            "DooD mode requires a `docker` group on the host. On Linux: "
            "`sudo usermod -aG docker $USER` (then log out + back in). "
            "If you don't actually need DooD, modify the instance and decline the prompt."
        )
    os.environ["DOCKER_GID"] = gid
    return {
        "compose_overrides": [PROJECT / "docker" / "compose.dood.yml"],
    }


MODE_HANDLERS = {
    "DooD": _apply_dood,
}


def apply_modes(modes):
    """Run each mode's handler and aggregate their contributions.
    Same shape as apply_tags. Unknown modes raise ValueError."""
    aggregated = {"compose_overrides": [], "volume_args": []}
    for mode in modes:
        if mode not in MODE_HANDLERS:
            raise ValueError(f"Unknown mode: {mode}. Known modes: {sorted(MODE_HANDLERS)}")
        contribution = MODE_HANDLERS[mode]()
        for key in aggregated:
            aggregated[key].extend(contribution.get(key, []))
    return aggregated
