"""Agent composition layer: parses the agent filename grammar
(`<name>([tag]|(parent))*`), locates the matching `.md`, picks the right `.conf`,
sorts agents by model family/version, and computes the docker build chain
(base → tags → modes) plus the per-handler runtime contributions (volume mounts +
side effects) via apply_composition.

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

# === Tag and mode names — the source-of-truth string identifiers for `[<tag>]` and `{<mode>}`.
# ORDERED_* lists declare priority (chain order, prompt order, label render order); each
# walrus also publishes the matching TAG_*/MODE_* module-level constant. Adding a new
# tag/mode means appending one line here AND wiring its handler in *_HANDLERS below. ===

ORDERED_TAGS = [
    TAG_PROG  := "prog",
]

ORDERED_MODES = [
    MODE_AUTO := "auto",
    MODE_DOOD := "DooD",
]

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
    falls back to DEFAULT_CONF. Tags are ignored here — handled by apply_composition()."""
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
    """[prog] tag handler. Two responsibilities — the side effects run *first*,
    then the dict of runtime extras is returned:

      • SIDE EFFECTS: prepare_caches() mkdirs the host cache dirs (so the
        bind-mount targets exist before the container starts; otherwise Docker
        creates them as root and we can't clean them up later); prune_caches()
        opportunistically trims oversized caches.
      • RETURNS: volume_args list of -v flags mounting those caches into the
        container at the matching paths.

    The compose/Dockerfile pair (compose.prog.yml + docker/Dockerfile.prog) is
    NOT selected here — chain order in compute_chain handles that."""
    prepare_caches()
    prune_caches()
    return {
        "volume_args": [arg for h, c in CACHE_MOUNTS.items() for arg in ("-v", f"{h}:{c}")],
    }


TAG_HANDLERS = {TAG_PROG: _apply_prog}   # ordering for tags lives in ORDERED_TAGS; this dict is a pure lookup


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
    """{DooD} mode: bind-mount the host's /var/run/docker.sock so the agent can drive
    the host's Docker daemon (run sub-containers, build images, etc.). Detects the
    host's docker-group GID via getent and exports it as DOCKER_GID so compose can
    pass it as a build-arg. Without a docker group on the host, the agent couldn't
    access the bind-mounted socket — so we fail loudly here rather than build an
    image that won't work."""
    gid = _detect_docker_gid()
    if gid is None:
        raise RuntimeError(
            "DooD mode requires a `docker` group on the host. On Linux: "
            "`sudo usermod -aG docker $USER` (then log out + back in). "
            "If you don't actually need DooD, modify the instance and decline the prompt."
        )
    os.environ["DOCKER_GID"] = gid
    return {}


def _apply_auto():
    """{auto} mode: lets the agent run unattended (--dangerously-skip-permissions
    is added in compose.auto.yml's entrypoint) behind an iptables outbound allowlist.
    The Dockerfile.auto image carries iptables + sudo + a tightly-scoped sudoers
    entry; the firewall script + entrypoint wrapper get bind-mounted via compose.auto.yml.
    No host-side side effects."""
    return {}


# Ordering for modes lives in ORDERED_MODES (defined up top with the constants);
# this dict is a pure lookup from name → handler.
MODE_HANDLERS = {
    MODE_AUTO: _apply_auto,
    MODE_DOOD: _apply_dood,
}


# === Chain composition: the build/run image is layered base → tags (in ORDERED_TAGS order) → modes (in ORDERED_MODES order). ===

def compute_chain(tags, modes):
    """Return the build chain for the given tags + modes. Always starts with 'base';
    appends tags in ORDERED_TAGS order, then modes in ORDERED_MODES order. Result
    drives both image naming (claude-agents:<chain[1:] joined by dot>, or
    claude-agents:base for chain == ['base']) and the compose -f stack. Unknown
    tags/modes raise ValueError so a typo surfaces loudly.

    `tags` / `modes` accept any iterable; coerced to sets internally for O(1)
    membership checks and natural deduplication."""
    tags, modes = set(tags), set(modes)
    if unknown := tags - set(ORDERED_TAGS):
        raise ValueError(f"Unknown tag(s): {sorted(unknown)}. Known tags: {ORDERED_TAGS}")
    if unknown := modes - set(ORDERED_MODES):
        raise ValueError(f"Unknown mode(s): {sorted(unknown)}. Known modes: {ORDERED_MODES}")
    chain = ["base"]
    for tag in ORDERED_TAGS:
        if tag in tags:
            chain.append(tag)
    for mode in ORDERED_MODES:
        if mode in modes:
            chain.append(mode)
    return chain


def apply_composition(chain):
    """Run handlers for each non-base step in the chain and aggregate their contributions.
    Returns {volume_args: [str,...]}. Tag steps look up TAG_HANDLERS, mode steps MODE_HANDLERS;
    'base' has no handler and is skipped. Unknown steps raise ValueError — chains coming from
    compute_chain are pre-validated, so this only fires if the caller built a chain by hand.

    NOTE: handlers may have side effects (host-side mkdirs, env-var exports for
    build-args, etc.) — see each `_apply_*` docstring. Calling apply_composition
    therefore both *gathers* runtime extras AND *triggers* any pre-build/pre-run
    setup the chain implies."""
    aggregated = {"volume_args": []}
    for step in chain[1:]:
        handler = TAG_HANDLERS.get(step) or MODE_HANDLERS.get(step)
        if handler is None:
            raise ValueError(f"Unknown chain step: {step}. Known: {list(TAG_HANDLERS) + list(MODE_HANDLERS)}")
        contribution = handler()
        for key in aggregated:
            aggregated[key].extend(contribution.get(key, []))
    return aggregated


def chain_image_tag(chain):
    """The docker image tag for a chain. ['base'] → 'claude-agents:base'.
    ['base', 'prog', 'auto'] → 'claude-agents:prog.auto' (lowercase to match
    the lowercase compose/Dockerfile filenames)."""
    if len(chain) == 1:
        return "claude-agents:base"
    return "claude-agents:" + ".".join(step.lower() for step in chain[1:])


def chain_compose_files(chain):
    """The compose `-f <path>` arg list for a chain. Always includes compose.yml;
    adds compose.<step>.yml (lowercased) for each non-base step in order."""
    args = ["-f", str(PROJECT / "docker" / "compose.yml")]
    for step in chain[1:]:
        args += ["-f", str(PROJECT / "docker" / f"compose.{step.lower()}.yml")]
    return args
