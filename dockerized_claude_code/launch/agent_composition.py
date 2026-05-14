"""Agent composition layer: computes the docker build chain (base → modifiers in
InstanceModifiers declaration order) and runs each active modifier's handler
contributions (volume mounts + compose env staging) in a single pass via
compose_chain. Also owns the mode-conditional memory-template list
(mode_memory_templates) that agents_crud.sync_memory_templates consumes.

The modifier taxonomy itself (InstanceModifiers + tags() / modes() views +
descriptions) lives in structs.py — both this module and agents_crud consume it
from there. Sort keys for the picker (agent/mode/tag sort) live in agents_crud —
they're picker-side concerns and don't belong in the composition layer.

Imports path constants from paths, the low-level file_access primitive
delete_file + ensure_dir (used by prune_caches / prepare_caches),
env-/mount-staging helpers from docker_config, the {auto}-mode firewall
entry points from network, and the InstanceModifiers taxonomy from structs;
agents_crud, menu_picker, and run.py import from here.
"""

import subprocess
import time

from .file_access import delete_file, ensure_dir
from .docker_config import DOCKER_GID, add_docker_mount, stage_compose_env
from .network import clear_firewall_status_files, start_whitelist_resolution
from .paths import ADDENDUM_SUFFIX, CACHE_MOUNTS, CACHE_ROOT
from .structs import InstanceModifiers

# === Priority orderings ===
# The InstanceModifiers enum (in structs.py) is the canonical ordered taxonomy
# — declaration order encodes chain composition order, and tags() / modes()
# provide the two subset views. Adding a new tag/mode means one line in that
# enum AND wiring its handler in *_HANDLERS below.

# === [prog]-tag cache pruning thresholds — applied to CACHE_MOUNTS by prune_caches below ===
CACHE_PRUNE_THRESHOLD_GB = 5   # per-cache size at which prune kicks in
CACHE_PRUNE_MIN_AGE_DAYS = 7   # files younger than this are kept even when over threshold

# === Always-allowed domains in {auto} mode (moved to launch/network.py) ===
# BUILTIN_FIREWALL_DOMAINS + start_whitelist_resolution live in
# launch/network.py — they're network-layer policy, not composition. This
# module just calls the entry points from _apply_auto below.

# === Tag dispatch: each handler returns the extras its tag contributes to the docker compose run ===

def prepare_caches():
    """Pre-create shared cache dirs so Docker doesn't auto-create them as root."""
    for host in CACHE_MOUNTS:
        ensure_dir(host)


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
                delete_file(f)
                freed += s.st_size
        if freed:
            print(f"  Pruned {host.relative_to(CACHE_ROOT)}: freed {freed / 1024**3:.1f} GB (was {total / 1024**3:.1f} GB)")


def _apply_prog():
    """[prog] tag handler. Three side effects, no return value:
      • prepare_caches() mkdirs the host cache dirs (so the bind-mount targets
        exist before the container starts; otherwise Docker creates them as
        root and we can't clean them up later).
      • prune_caches() opportunistically trims oversized caches.
      • add_docker_mount stages each cache as a bind-mount for the upcoming
        `docker compose run` (read-write — toolchains write into them).

    The compose/Dockerfile pair (compose.prog.yml + docker/Dockerfile.prog) is
    NOT selected here — chain order in compose_chain handles that."""
    prepare_caches()
    prune_caches()
    for host, container in CACHE_MOUNTS.items():
        add_docker_mount(host, container)


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
    host's docker-group GID via getent and stages it as the DOCKER_GID compose var
    so the compose layer picks it up as a build-arg. Without a docker group on
    the host, the agent couldn't access the bind-mounted socket — so we fail loudly
    here rather than build an image that won't work."""
    gid = _detect_docker_gid()
    if gid is None:
        raise RuntimeError(
            f"{InstanceModifiers.MODE_DOOD.value} mode requires a `docker` group on the host. On Linux: "
            f"`sudo usermod -aG docker $USER` (then log out + back in). "
            f"If you don't actually need {InstanceModifiers.MODE_DOOD.value}, modify the instance and decline the prompt."
        )
    stage_compose_env(DOCKER_GID, gid)


def _apply_auto(state_dir):
    """{auto} mode: kick off the two-phase firewall whitelist resolve so it
    overlaps with `docker compose build` (which ensure_image fires right
    after compose_chain returns). Two steps:
      1. Wipe any stale status files left on disk from a previous run on
         this instance — so the agent (and the host-side debug view) never
         observe leftover content while we're seeding the fresh state.
      2. Fire start_whitelist_resolution(state_dir): Phase 1 (critical
         Anthropic DNS) runs synchronously from the caller's perspective
         (docker_config.run_compose blocks on wait_for_critical_addresses
         before staging WHITELIST_ADDRESSES); Phase 2 (rest) streams in
         the background and feeds the firewall updater spawned just before
         `docker compose run`.
    No bind-mount work — the agent-visible status file lives at
    `<state_dir>/domains_pending_resolve.yml`, which the per-instance
    state-dir bind-mount in set_container_mounts already exposes inside
    the container at /home/claude/.claude/."""
    clear_firewall_status_files(state_dir)
    start_whitelist_resolution(state_dir)


# === Mode-conditional memory addendums ===
# Each mode optionally has a `memory/<mode_lower>-addendum.md` template that
# agents_crud.sync_memory_templates splices into per-instance MEMORY.md when
# the mode is active (and removes when it's not). Missing template files are
# no-ops in sync_memory_templates, so a mode without an addendum costs nothing.

def mode_memory_templates(modes):
    """Build `(filename, active)` pairs for each `<mode>-addendum.md` template
    in MEMORY_DIR, one entry per InstanceModifiers.modes() member — `active` is
    True iff that mode's `.value` is in `modes` (which holds canonical strings
    as stored in agent_modes_map.json). agents_crud.sync_memory_templates
    processes the returned list after the always-active seek_summary entry."""
    return [(f"{m.filename_form}{ADDENDUM_SUFFIX}", m.value in modes)
            for m in InstanceModifiers.modes()]


# === Chain composition: the build/run image is layered base → modifiers in InstanceModifiers declaration order. ===

def compose_chain(tags, modes, state_dir):
    """Run each active modifier's handler and compose the docker build chain.
    Two independent steps: (1) invoke `_apply_<modifier>()` for each active
    modifier — side effects: stage compose env vars / bind-mounts, kick off
    the {auto}-mode background DNS resolve, etc.; (2) build the chain list in
    InstanceModifiers declaration order. Always starts with 'base'.

    `state_dir` is the per-instance host path that gets bind-mounted into
    the container — passed in for {auto}-mode (status files live inside it);
    other handlers don't read it. write_text inside the network module auto-
    creates this dir, so it's safe to use here before setup_state runs.

    Returns the chain list of strings — drives image naming
    (claude-agents:<chain[1:] joined by dot>, or claude-agents:base for the
    base case) and the compose -f stack via chain_image_tag /
    chain_compose_files in docker_config.

    Unknown tags/modes raise ValueError so a typo surfaces loudly. `tags` /
    `modes` accept any iterable of canonical-string forms; coerced to sets
    internally for O(1) membership checks and natural deduplication.

    Adding a new modifier means: a new entry in InstanceModifiers, the
    matching `_apply_*` function above, and one new conditional in step (1)
    here. Step (2) picks it up automatically — it iterates InstanceModifiers."""
    tags, modes = set(tags), set(modes)
    if unknown := tags - set(InstanceModifiers.tag_values()):
        raise ValueError(f"Unknown tag(s): {sorted(unknown)}. Known tags: {list(InstanceModifiers.tag_values())}")
    if unknown := modes - set(InstanceModifiers.mode_values()):
        raise ValueError(f"Unknown mode(s): {sorted(unknown)}. Known modes: {list(InstanceModifiers.mode_values())}")

    # (1) Side-effect dispatch — each active modifier stages its contributions.
    if InstanceModifiers.TAG_PROG.value in tags:
        _apply_prog()
    if InstanceModifiers.MODE_AUTO.value in modes:
        _apply_auto(state_dir)
    if InstanceModifiers.MODE_DOOD.value in modes:
        _apply_dood()

    # (2) Chain construction — iterate the source of truth for ordering and
    # pick whichever modifiers are active. Order in the chain follows
    # InstanceModifiers declaration order, regardless of input order.
    active = tags | modes
    return ["base"] + [m.value for m in InstanceModifiers if m.value in active]
