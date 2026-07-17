"""Per-tag launch-side handlers: runs each active tag's host-side
contributions (volume mounts + compose env staging + background kickoffs) in
a single pass via compose_chain, which also returns the docker build chain.

The tag taxonomy lives in the tags package (tree-discovered members); this
module holds the *dynamic* side of a tag's launch behavior — the parts that
can't be declarative data (cache pruning, GID detection, DNS kickoff). The
docker flip (tag.docker) will absorb the static mounts declared here.

Dispatch is by naming convention — `_apply_<tag name>` looked up in this
module per chain entry; tags without a handler are a no-op (data-only tags
are legal). run.py imports compose_chain from here.
"""

import time

from .compose_env import ComposeEnvKey, stage_compose_env
from .docker_config import (
    add_docker_mount, detect_docker_gid, docker_check_any_agent_running_subprocess,
)
from .file_access import (
    ensure_dir, iter_file_stats, path_exists, remove_path,
)
from .network import start_whitelist_resolution
from .paths import (
    CACHE_MOUNTS, CACHE_ROOT, DOCKER_AUTO_MOUNTS, DOCKER_DOOD_MOUNTS,
)
from .tags import Instance

# === [code]-tag cache pruning thresholds — applied to CACHE_MOUNTS by prune_caches below ===
CACHE_PRUNE_THRESHOLD_GB = 5   # per-cache size at which prune kicks in
CACHE_PRUNE_MIN_AGE_DAYS = 7   # files younger than this are kept even when over threshold
SECONDS_PER_DAY = 86400        # used by prune_caches to convert MIN_AGE_DAYS into an epoch-seconds cutoff


# === Tag dispatch: each handler returns the extras its tag contributes to the docker compose run ===

def prepare_caches() -> None:
    """Pre-create shared cache dirs so Docker doesn't auto-create them as root."""
    for host in CACHE_MOUNTS:
        ensure_dir(host)


def prune_caches() -> None:
    """For caches above CACHE_PRUNE_THRESHOLD_GB, remove files older than CACHE_PRUNE_MIN_AGE_DAYS.
    Skipped when any agent container is running (to avoid yanking caches mid-build)."""
    if docker_check_any_agent_running_subprocess():
        return
    time_cutoff = time.time() - CACHE_PRUNE_MIN_AGE_DAYS * SECONDS_PER_DAY  # days → seconds (match epoch-second time.time())
    size_cutoff = CACHE_PRUNE_THRESHOLD_GB * 1024**3                        # GB   → bytes   (match st_size units)
    for host in CACHE_MOUNTS:
        if not path_exists(host):
            continue
        files = list(iter_file_stats(host))
        total = sum(size for _, size, _ in files)
        if total <= size_cutoff:
            continue
        freed = 0
        for f, size, mtime in files:
            if mtime < time_cutoff:
                remove_path(f)
                freed += size
        if freed:
            print(f"  Pruned {host.relative_to(CACHE_ROOT)}: freed {freed / 1024**3:.1f} GB (was {total / 1024**3:.1f} GB)")


def _apply_code(inst: Instance) -> None:
    """[code] handler. Three side effects, no return value:
      • prepare_caches() mkdirs the host cache dirs (so the bind-mount targets
        exist before the container starts; otherwise Docker creates them as
        root and we can't clean them up later).
      • prune_caches() opportunistically trims oversized caches.
      • add_docker_mount stages each cache as a bind-mount for the upcoming
        container run (read-write — toolchains write into them).

    `inst` is unused here — every `_apply_*` handler shares the same
    signature so compose_chain can dispatch them uniformly by name."""
    prepare_caches()
    prune_caches()
    for host, container in CACHE_MOUNTS.items():
        add_docker_mount(host, container)


def _apply_dood(inst: Instance) -> None:
    """{dood} handler: bind-mount the host's /var/run/docker.sock (via
    DOCKER_DOOD_MOUNTS) so the agent can drive the host's Docker daemon.
    Looks up the host's docker-group GID via detect_docker_gid and stages it
    as the DOCKER_GID build-arg so the in-image `docker` group matches the
    host's. Without a docker group the socket would be unreadable — fail
    loudly here rather than build an image that won't work."""
    gid = detect_docker_gid()
    if gid is None:
        raise RuntimeError(
            "{dood} requires a `docker` group on the host. On Linux: "
            "`sudo usermod -aG docker $USER` (then log out + back in). "
            "If you don't actually need {dood}, modify the instance and untick it."
        )
    stage_compose_env(ComposeEnvKey.DOCKER_GID, gid)
    for source, target in DOCKER_DOOD_MOUNTS.items():
        add_docker_mount(source, target)


def _apply_auto(inst: Instance) -> None:
    """{auto} handler: kick off the two-phase firewall whitelist resolve so it
    overlaps with the image build (run.py fires ensure_image right after
    compose_chain returns), then stage the firewall script + entrypoint
    wrapper bind-mounts from DOCKER_AUTO_MOUNTS. The agent-visible status
    file (`domains_pending_resolve.yml`) lives in the state dir, already
    exposed via set_container_mounts' per-instance mount.

    (The firewall machinery still rides {auto} until the docker flip extracts
    it into the standalone {firewall} specialty.)"""
    start_whitelist_resolution(inst.state_dir)
    for source, target in DOCKER_AUTO_MOUNTS.items():
        add_docker_mount(source, target)


# === Chain composition ===

def compose_chain(inst: Instance) -> list[str]:
    """Run each active tag's handler and return the docker build chain
    (`inst.chain` — base first, professions in requirement order, then
    specialties). Handlers are side-effect-only: stage env vars/bind-mounts,
    kick off the {auto} DNS resolve, etc.

    Dispatch is by naming convention — `_apply_<tag name>` looked up in this
    module per chain entry (so tests can patch individual handlers). A tag
    without a handler is a NO-OP: data-only tags ([web]'s playwright cache
    rides [code]'s ~/.cache mount; policies never join the chain) are legal
    and need no code here.

    The chain list drives image naming (chain_image_tag) and the compose
    layer stack (chain_compose_files) in docker_config."""
    chain = inst.chain
    for name in chain:
        handler = globals().get(f"_apply_{name}")
        if handler is not None:
            handler(inst)
    return chain
