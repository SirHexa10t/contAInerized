"""Agent modifier handling: computes the docker build chain (InstanceModifiers
in declaration order — BASE always, then user-active tags/modes) and runs each
active modifier's handler contributions (volume mounts + compose env staging)
in a single pass via compose_chain. Also owns:
  - the dangerous-combination warning (warn_if_dangerous_modes) that agents_crud's
    writers call after persisting a new mode set;
  - the modifier-prompting dispatch (prompt_for_modes + prompt_modifier) — header
    / body copy lives in template_code/modifier_prompts.py; this module owns the
    "which modes to ask, with what applicability gating" logic.

The modifier taxonomy itself (InstanceModifiers + tags() / modes() views +
descriptions) lives in structs.py — both this module and agents_crud consume it
from there. Sort keys for the picker (agent/mode/tag sort) live in agents_crud —
they're picker-side concerns and don't belong in the modifier-handling layer.

Imports path constants from paths, the file_access primitives needed by
prune_caches / prepare_caches (ensure_dir, iter_file_stats, path_exists,
remove_path), env-/mount-staging helpers + docker subprocess wrappers from
docker_config, the {auto}-mode firewall entry points from network, the
InstanceModifiers taxonomy from structs, prompt copy from template_code, and
prompt_yn from utils; agents_crud, menu_picker, and run.py import from here.
"""

import time
from collections.abc import Iterable

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
from .structs import InstanceModifiers
from .template_code.modifier_prompts import MODIFIER_NOTICE_PROMPTS, MODIFIER_YN_PROMPTS
from .utils import prompt_keypress, prompt_yn

# === Modifier taxonomy + chain-composition ordering ===
# The InstanceModifiers enum (in structs.py) is the canonical ordered taxonomy
# — declaration order encodes chain composition order, and tags() / modes()
# provide the two subset views. Adding a new tag/mode means one line in that
# enum AND wiring its `_apply_<x>()` handler into `compose_chain` below.

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


def _apply_code() -> None:
    """[code] tag handler. Three side effects, no return value:
      • prepare_caches() mkdirs the host cache dirs (so the bind-mount targets
        exist before the container starts; otherwise Docker creates them as
        root and we can't clean them up later).
      • prune_caches() opportunistically trims oversized caches.
      • add_docker_mount stages each cache as a bind-mount for the upcoming
        `docker compose run` (read-write — toolchains write into them).

    The compose/Dockerfile pair (compose.code.yml + docker/Dockerfile.code) is
    NOT selected here — chain order in compose_chain handles that."""
    prepare_caches()
    prune_caches()
    for host, container in CACHE_MOUNTS.items():
        add_docker_mount(host, container)


# === Mode dispatch — like tags, but per-instance (set at create/modify time, stored in agent_modes_map.json) ===

def _apply_dood() -> None:
    """{DooD} mode: bind-mount the host's /var/run/docker.sock (via DOCKER_DOOD_MOUNTS)
    so the agent can drive the host's Docker daemon (run sub-containers, build images,
    etc.). Looks up the host's docker-group GID via docker_config.detect_docker_gid
    and stages it as the DOCKER_GID compose var so the compose layer picks it up as a
    build-arg. Without a docker group on the host, the agent couldn't access the
    bind-mounted socket — so we fail loudly here rather than build an image that
    won't work."""
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


def _apply_web() -> None:
    """{web} mode: no per-launch side effects. Playwright's default
    browser-install location (`~/.cache/ms-playwright/`) sits under the
    `~/.cache` mount that [code]'s `_apply_code` already stages, so the
    host cache is shared across every [code][web] instance with no extra
    plumbing. This handler exists for the test_essential_files contract
    (every non-BASE modifier has an `_apply_<slug>` callable)."""
    pass


def _apply_auto(state_dir) -> None:
    """{auto} mode: kick off the two-phase firewall whitelist resolve so it
    overlaps with `docker compose build` (which ensure_image fires right
    after compose_chain returns), then stage the firewall script + entrypoint
    wrapper bind-mounts. Two steps:
      1. Fire start_whitelist_resolution(state_dir): clears any stale status
         from a previous run on this instance, then runs Phase 1 (critical
         Anthropic DNS — synchronous from the caller's perspective, since
         docker_config.run_compose blocks on wait_for_critical_addresses
         before staging WHITELIST_ADDRESSES) and kicks off Phase 2 (rest)
         streaming in the background to feed the firewall updater spawned
         just before `docker compose run`.
      2. Stage the bind-mounts from DOCKER_AUTO_MOUNTS — init-firewall.sh
         + auto-entrypoint.sh get mounted under /usr/local/bin/ inside the
         container. The agent-visible status file (`domains_pending_resolve.yml`)
         lives in the state dir and is already exposed via set_container_mounts'
         per-instance mount, so no extra plumbing for it."""
    start_whitelist_resolution(state_dir)
    for source, target in DOCKER_AUTO_MOUNTS.items():
        add_docker_mount(source, target)


# === Dangerous-combination warning ===
# Lives here because this module owns modifier-combination semantics: which
# modes mean what, how they compose, and which combinations need user-visible
# guardrails. agents_crud calls warn_if_dangerous_modes after persisting a
# fresh mode set (set_instance_modes / modify_instance) — agents_crud just
# writes state; the "is this combination dangerous?" judgement is here.

def warn_if_dangerous_modes(modes: Iterable[InstanceModifiers]) -> None:
    """For each `MODIFIER_NOTICE_PROMPTS` entry whose combination is a subset
    of `modes`, fire `prompt_keypress` with its (header, body). One warning +
    press-any-key per matching combo — same dispatch shape as
    `prompt_for_modes` does for `MODIFIER_YN_PROMPTS` via `prompt_modifier`.
    No-op when no dangerous combo is present, so callers can fire this
    unconditionally after any mode-set write."""
    active = set(modes)
    for combo, (header, body) in MODIFIER_NOTICE_PROMPTS.items():
        if combo <= active:
            prompt_keypress(header=header, body=body)


# === Mode-prompt dispatch ===
# The "which modes to ask, with what applicability gating" logic — header /
# body copy per modifier lives in template_code/modifier_prompts.py. The
# picker exposes prompt_for_modes via a thin menu_picker.prompt_modes wrapper
# so run.py / select_agent's modify flow can call it without importing this
# module directly.

def prompt_modifier(modifier: InstanceModifiers, current_modifiers) -> bool:
    """Y/N prompt for opting into `modifier`. Header + body copy looked up in
    `MODIFIER_YN_PROMPTS`; prompt label comes from the modifier's `.label`.
    `current_modifiers` is an iterable of canonical-string modifier names
    (tags + currently-active modes) used to pre-fill the Y/N default (True
    iff `modifier.value` is in there)."""
    header, body = MODIFIER_YN_PROMPTS[modifier]
    return prompt_yn(
        header=header,
        body=body,
        prompt_label=modifier.label,
        default=modifier.value in current_modifiers,
    )


def prompt_for_modes(tags: tuple[InstanceModifiers, ...], current_modes: tuple[InstanceModifiers, ...] = ()) -> list[InstanceModifiers]:
    """Prompt for each mode whose prerequisites are satisfied by the agent's
    `tags`, in InstanceModifiers declaration order. `current_modes` pre-fills
    the Y/N defaults — empty for new instances. Returns the newly-selected
    modes in declaration order. Exposed to callers via the
    menu_picker.prompt_modes wrapper."""
    current_modifiers = [m.value for m in (*tags, *current_modes)]
    tag_set = set(tags)
    new_modes: list[InstanceModifiers] = []
    for mode in InstanceModifiers.in_order(MODIFIER_YN_PROMPTS):
        if mode.applies_to(tag_set) and prompt_modifier(mode, current_modifiers):
            new_modes.append(mode)
    return new_modes


# === Chain composition: the build/run image is layered base → modifiers in InstanceModifiers declaration order. ===

def compose_chain(inst_id) -> list[str]:
    """Run each active modifier's handler and return the docker build chain.
    Accesses `inst_id.chain` once — that's where the validation (typo'd
    tags / stale modes) lives, and it's the canonical modifier-value tuple
    in InstanceModifiers declaration order (BASE first, then user-active
    tags + modes). Dispatch then runs `_apply_<modifier>()` for each
    user-toggleable modifier whose value is in the chain — side effects
    only: stage compose env vars / bind-mounts, kick off the {auto}-mode
    background DNS resolve, etc. BASE has no handler (no side effects
    beyond being the starting image).

    inst_id.state_dir is the per-instance host path bind-mounted into the
    container — passed to _apply_auto for the {auto}-mode status file
    location; other handlers don't read it. write_text inside the network
    module auto-creates this dir, so it's safe to use here before
    setup_state runs.

    Returns the chain list of strings — drives image naming
    (claude-agents:<chain[1:] joined by dot>, or claude-agents:base for the
    base case) and the compose -f stack via chain_image_tag /
    chain_compose_files in docker_config.

    Adding a new user-toggleable modifier means: a new entry in
    InstanceModifiers, the matching `_apply_*` function above, and one new
    conditional below. inst_id.chain picks it up automatically — it
    iterates InstanceModifiers."""
    chain = inst_id.chain   # validates against InstanceModifiers taxonomy

    if InstanceModifiers.TAG_CODE.value in chain:
        _apply_code()
    if InstanceModifiers.MODE_WARN_AUTO.value in chain:
        _apply_auto(inst_id.state_dir)
    if InstanceModifiers.MODE_WARN_DOOD.value in chain:
        _apply_dood()
    if InstanceModifiers.MODE_WEB.value in chain:
        _apply_web()

    return list(chain)
