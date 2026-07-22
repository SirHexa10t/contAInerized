"""Per-tag launch-side handlers: `apply_tags` runs one pass over the
instance's active tags — staging every declarative `tag.docker` mount, then
dispatching each chain entry's dynamic handler — and returns the chain.

The tag taxonomy lives in the tags package (tree-discovered members); this
module holds the *dynamic* side of a tag's launch behavior — the parts that
can't be declarative data (cache pruning, GID detection, DNS kickoff). The
static side (mounts, cap_add, entrypoint, arg/env forwards) is declared in
each tag's `tag.docker` and consumed here (mounts) + in docker_config
(everything else).

Dispatch is by naming convention — `_apply_<tag name>` looked up in this
module per chain entry; tags without a handler are a no-op (data-only tags
like {auto} — claude_args only — are legal). run.py imports apply_tags
from here.
"""

import time

from .container_env import ContainerEnvKey, stage_container_env
from .docker_config import (
    add_docker_mount, detect_docker_gid, docker_check_any_agent_running_subprocess,
)
from .file_access import (
    ensure_dir, iter_file_stats, path_exists, remove_path,
)
from .network import start_whitelist_resolution
from .paths import CACHE_MOUNTS, CACHE_ROOT
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
    signature so apply_tags can dispatch them uniformly by name."""
    prepare_caches()
    prune_caches()
    for host, container in CACHE_MOUNTS.items():
        add_docker_mount(host, container)


def _apply_dood(inst: Instance) -> None:
    """{dood} handler — the dynamic half (the docker-socket mount is
    declarative, in `_dood/tag.docker`): look up the host's docker-group GID
    via detect_docker_gid and stage it as the DOCKER_GID build-arg so the
    in-image `docker` group matches the host's. Without a docker group the
    socket would be unreadable — fail loudly here rather than build an image
    that won't work."""
    gid = detect_docker_gid()
    if gid is None:
        raise RuntimeError(
            "{dood} requires a `docker` group on the host. On Linux: "
            "`sudo usermod -aG docker $USER` (then log out + back in). "
            "If you don't actually need {dood}, modify the instance and untick it."
        )
    stage_container_env(ContainerEnvKey.DOCKER_GID, gid)


def _apply_firewall(inst: Instance) -> None:
    """{firewall} handler — the dynamic half (script/entrypoint mounts,
    NET_ADMIN, and the WHITELIST_ADDRESSES forward are declarative, in the
    specialty's tag.docker): kick off the two-phase whitelist resolve so it
    overlaps with the image build (run.py fires ensure_image right after
    apply_tags returns). The agent-visible status file
    (`domains_pending_resolve.yml`) lives in the state dir, already exposed
    via set_container_mounts' per-instance mount."""
    start_whitelist_resolution(inst.state_dir)


# === Tag application ===

def apply_tags(inst: Instance) -> list[str]:
    """Apply every active tag's launch-side contribution and return the chain
    (`inst.chain` — base first, professions in requirement order, then
    specialties). Two passes:
      1. Declarative: stage each tag.docker mount via add_docker_mount
         (sources were resolved + existence-checked at scan time).
      2. Dynamic: dispatch each chain entry's `_apply_<tag name>` handler —
         side-effect-only (cache prep, GID staging, the {firewall} DNS
         kickoff). Looked up in this module per entry (so tests can patch
         individual handlers); a tag without a handler is a NO-OP: data-only
         tags ({auto}'s claude_args, [webdev]'s playwright cache riding [code]'s
         ~/.cache mount) are legal and need no code here.

    The returned chain drives the addendum composition; image naming/building
    runs off `inst.build_steps` in docker_config."""
    for contribution in inst.docker_contributions:
        for source, target in contribution.mounts:
            add_docker_mount(source, target)
    chain = inst.chain
    for name in chain:
        handler = globals().get(f"_apply_{name}")
        if handler is not None:
            handler(inst)
    return chain
