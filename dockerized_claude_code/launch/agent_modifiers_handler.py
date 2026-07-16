"""Agent modifier handling: computes the docker build chain (InstanceModifiers
in declaration order — BASE always, then user-active tags/modes) and runs each
active modifier's handler contributions (volume mounts + compose env staging)
in a single pass via compose_chain.

Mode *selection* UI lives in menu_picker (prompt_modes' checkbox form, fed by
template_code/modifier_prompts.py copy — including the dangerous-combination
warnings rendered live in the form). The modifier taxonomy itself
(InstanceModifiers + tags() / modes() views + descriptions) lives in structs.py.
Sort keys for the picker (agent/mode/tag sort) live in agents_crud — they're
picker-side concerns and don't belong in the modifier-handling layer.

Imports path constants from paths, the file_access primitives needed by
prune_caches / prepare_caches (ensure_dir, iter_file_stats, path_exists,
remove_path), env-/mount-staging helpers + docker subprocess wrappers from
docker_config, the {auto}-mode firewall entry points from network, and the
InstanceModifiers taxonomy from structs; run.py imports compose_chain from here.
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
from .structs import InstanceIdentity, InstanceModifiers

# === Modifier taxonomy + chain-composition ordering ===
# The InstanceModifiers enum (in structs.py) is the canonical ordered taxonomy
# — declaration order encodes chain composition order, and tags() / modes()
# provide the two subset views. Adding a new tag/mode means one line in that
# enum AND a matching `_apply_<value>()` handler below — compose_chain
# dispatches by naming convention (test_essential_files enforces the pairing),
# so there is no dispatch table or conditional ladder to update.

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


def _apply_code(inst_id: InstanceIdentity) -> None:
    """[code] tag handler. Three side effects, no return value:
      • prepare_caches() mkdirs the host cache dirs (so the bind-mount targets
        exist before the container starts; otherwise Docker creates them as
        root and we can't clean them up later).
      • prune_caches() opportunistically trims oversized caches.
      • add_docker_mount stages each cache as a bind-mount for the upcoming
        `docker compose run` (read-write — toolchains write into them).

    `inst_id` is unused here — every `_apply_*` handler shares the same
    signature so compose_chain can dispatch them uniformly by naming
    convention.

    The compose/Dockerfile pair (compose.code.yml + docker/Dockerfile.code) is
    NOT selected here — chain order in compose_chain handles that."""
    prepare_caches()
    prune_caches()
    for host, container in CACHE_MOUNTS.items():
        add_docker_mount(host, container)


# === Mode dispatch — like tags, but per-instance (set at create/modify time, stored in agent_modes_map.json) ===

def _apply_dood(inst_id: InstanceIdentity) -> None:
    """{DooD} mode: bind-mount the host's /var/run/docker.sock (via DOCKER_DOOD_MOUNTS)
    so the agent can drive the host's Docker daemon (run sub-containers, build images,
    etc.). Looks up the host's docker-group GID via docker_config.detect_docker_gid
    and stages it as the DOCKER_GID compose var so the compose layer picks it up as a
    build-arg. Without a docker group on the host, the agent couldn't access the
    bind-mounted socket — so we fail loudly here rather than build an image that
    won't work. `inst_id` is unused — uniform `_apply_*` handler signature."""
    gid = detect_docker_gid()
    if gid is None:
        raise RuntimeError(
            f"{InstanceModifiers.MODE_WARN_DOOD.value} mode requires a `docker` group on the host. On Linux: "
            f"`sudo usermod -aG docker $USER` (then log out + back in). "
            f"If you don't actually need {InstanceModifiers.MODE_WARN_DOOD.value}, modify the instance and decline the prompt."
        )
    stage_compose_env(ComposeEnvKey.DOCKER_GID, gid)
    for source, target in DOCKER_DOOD_MOUNTS.items():
        add_docker_mount(source, target)


def _apply_web(inst_id: InstanceIdentity) -> None:
    """{web} mode: no per-launch side effects. Playwright's default
    browser-install location (`~/.cache/ms-playwright/`) sits under the
    `~/.cache` mount that [code]'s `_apply_code` already stages, so the
    host cache is shared across every [code][web] instance with no extra
    plumbing. This handler exists for the test_essential_files contract
    (every non-BASE modifier has an `_apply_<slug>` callable). `inst_id`
    is unused — uniform `_apply_*` handler signature."""
    pass


def _apply_auto(inst_id: InstanceIdentity) -> None:
    """{auto} mode: kick off the two-phase firewall whitelist resolve so it
    overlaps with `docker compose build` (which ensure_image fires right
    after compose_chain returns), then stage the firewall script + entrypoint
    wrapper bind-mounts. Two steps:
      1. Fire start_whitelist_resolution(inst_id.state_dir): clears any stale
         status from a previous run on this instance, then runs Phase 1
         (critical Anthropic DNS — synchronous from the caller's perspective,
         since docker_config.run_compose blocks on wait_for_critical_addresses
         before staging WHITELIST_ADDRESSES) and kicks off Phase 2 (rest)
         streaming in the background to feed the firewall updater spawned
         just before `docker compose run`.
      2. Stage the bind-mounts from DOCKER_AUTO_MOUNTS — init-firewall.sh
         + auto-entrypoint.sh get mounted under /usr/local/bin/ inside the
         container. The agent-visible status file (`domains_pending_resolve.yml`)
         lives in the state dir and is already exposed via set_container_mounts'
         per-instance mount, so no extra plumbing for it."""
    start_whitelist_resolution(inst_id.state_dir)
    for source, target in DOCKER_AUTO_MOUNTS.items():
        add_docker_mount(source, target)


# === Chain composition: the build/run image is layered base → modifiers in InstanceModifiers declaration order. ===

def compose_chain(inst_id: InstanceIdentity) -> list[str]:
    """Run each active modifier's handler and return the docker build chain.
    Accesses `inst_id.chain` once — that's where the validation (typo'd
    tags / stale modes) lives, and it's the canonical modifier-value tuple
    in InstanceModifiers declaration order (BASE first, then user-active
    tags + modes). Dispatch then runs the matching `_apply_<value>` handler
    (looked up by naming convention — see below) for each user-toggleable
    modifier in the chain — side effects only: stage compose env vars /
    bind-mounts, kick off the {auto}-mode background DNS resolve, etc.
    BASE has no handler (no side effects beyond being the starting image).

    Every handler takes the InstanceIdentity (most ignore it; _apply_auto
    reads .state_dir for the {auto}-mode status file location). write_text
    inside the network module auto-creates that dir, so it's safe to use
    here before setup_state runs.

    Dispatch is by naming convention — `_apply_<modifier.value.lower()>`
    looked up in this module at call time (so tests can patch individual
    handlers). test_essential_files enforces the convention: every non-BASE
    member must have a matching handler, so a missing one fails at test
    time (and, defensively, as a loud KeyError here rather than a silent
    skip).

    Returns the chain list of strings — drives image naming
    (claude-agents:<chain[1:] joined by dot>, or claude-agents:base for the
    base case) and the compose -f stack via chain_image_tag /
    chain_compose_files in docker_config.

    Adding a new user-toggleable modifier means: a new entry in
    InstanceModifiers + the matching `_apply_<value>` function above —
    dispatch wires itself up from the name. inst_id.chain picks the member
    up automatically too — it iterates InstanceModifiers."""
    chain = inst_id.chain   # validates against InstanceModifiers taxonomy

    for modifier in InstanceModifiers:
        if modifier is InstanceModifiers.BASE or modifier.value not in chain:
            continue
        globals()[f"_apply_{modifier.value.lower()}"](inst_id)

    return list(chain)
