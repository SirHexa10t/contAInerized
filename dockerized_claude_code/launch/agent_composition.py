"""Agent composition layer: computes the docker build chain (base → modifiers in
InstanceModifiers declaration order) and runs each active modifier's handler
contributions (volume mounts + compose env staging) in a single pass via
compose_chain. Also owns the per-instance MEMORY.md reconcile (sync_memory_templates
+ its modifier_memory_templates feeder, both keyed off the modifier taxonomy)
and the dangerous-combination warning (warn_if_dangerous_modes) that
agents_crud's writers call after persisting a new mode set.

The modifier taxonomy itself (InstanceModifiers + tags() / modes() views +
descriptions) lives in structs.py — both this module and agents_crud consume it
from there. Sort keys for the picker (agent/mode/tag sort) live in agents_crud —
they're picker-side concerns and don't belong in the composition layer.

Imports path constants from paths, the file_access primitives needed by
prune_caches / prepare_caches + sync_memory_templates (ensure_dir,
iter_file_stats, path_exists, remove_path, read_text, write_text,
installed_cred_clis), env-/mount-staging helpers + docker subprocess wrappers
from docker_config, the {auto}-mode firewall entry points from network, the
InstanceModifiers taxonomy from structs, and splice_block from utils;
agents_crud, menu_picker, and run.py import from here.
"""

import sys
import time

from .file_access import (
    ensure_dir, installed_cred_clis, iter_file_stats, path_exists, read_text,
    remove_path, write_text,
)
from .docker_config import (
    ComposeEnvKey, add_docker_mount, any_agent_container_running,
    detect_docker_gid, stage_compose_env,
)
from .network import start_whitelist_resolution
from .paths import (
    CACHE_MOUNTS, CACHE_ROOT, DOCKER_AUTO_MOUNTS, DOCKER_DOOD_MOUNTS,
    SEEK_SUMMARY_PATH, memory_template_path, state_memory_path,
)
from .structs import InstanceModifiers
from .utils import splice_block

# Wrapper banner around `<name>-instructions-{start,end}` marker lines in the
# spliced MEMORY.md blocks — visual cue that the line is a machine-managed
# comment, not memory instructions for the agent.
MEMORY_BLOCK_WRAPPER_BANNER = "#" * 21

# === Modifier taxonomy + chain-composition ordering ===
# The InstanceModifiers enum (in structs.py) is the canonical ordered taxonomy
# — declaration order encodes chain composition order, and tags() / modes()
# provide the two subset views. Adding a new tag/mode means one line in that
# enum AND wiring its `_apply_<x>()` handler into `compose_chain` below.

# === [prog]-tag cache pruning thresholds — applied to CACHE_MOUNTS by prune_caches below ===
CACHE_PRUNE_THRESHOLD_GB = 5   # per-cache size at which prune kicks in
CACHE_PRUNE_MIN_AGE_DAYS = 7   # files younger than this are kept even when over threshold


# === Tag dispatch: each handler returns the extras its tag contributes to the docker compose run ===

def prepare_caches():
    """Pre-create shared cache dirs so Docker doesn't auto-create them as root."""
    for host in CACHE_MOUNTS:
        ensure_dir(host)


def prune_caches():
    """For caches above CACHE_PRUNE_THRESHOLD_GB, remove files older than CACHE_PRUNE_MIN_AGE_DAYS.
    Skipped when any agent container is running (to avoid yanking caches mid-build)."""
    if any_agent_container_running():
        return
    time_cutoff = time.time() - CACHE_PRUNE_MIN_AGE_DAYS * 86400  # days → seconds (match epoch-second time.time())
    size_cutoff = CACHE_PRUNE_THRESHOLD_GB * 1024**3              # GB   → bytes   (match st_size units)
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

def _apply_dood():
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
            f"{InstanceModifiers.MODE_DOOD.value} mode requires a `docker` group on the host. On Linux: "
            f"`sudo usermod -aG docker $USER` (then log out + back in). "
            f"If you don't actually need {InstanceModifiers.MODE_DOOD.value}, modify the instance and decline the prompt."
        )
    stage_compose_env(ComposeEnvKey.DOCKER_GID, gid)
    for source, target in DOCKER_DOOD_MOUNTS.items():
        add_docker_mount(source, target)


def _apply_auto(state_dir):
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

def warn_if_dangerous_modes(modes):
    """Stern red warning + press-any-key gate when `modes` contains a dangerous
    combination — currently just {auto}+{DooD}. {auto} drops Claude Code's
    permission prompts; {DooD} hands the agent the host's Docker daemon.
    Together the agent can do effectively anything on the host, unattended.
    Blocks until the user presses any key so the warning isn't silently
    scrolled past. No-op when the dangerous combo isn't present, so callers
    can fire this unconditionally after any mode-set write."""
    if not (InstanceModifiers.MODE_AUTO.value in modes
            and InstanceModifiers.MODE_DOOD.value in modes):
        return
    print()
    print(f"\033[1;31m  ⚠ YOU'VE ENABLED BOTH {InstanceModifiers.MODE_AUTO.label} AND {InstanceModifiers.MODE_DOOD.label} - PROCEED WITH CAUTION,")
    print("    AS THE AI AGENT HAS THE POWER TO DO ANYTHING ON YOUR COMPUTER,")
    print("    AND DOESN'T REQUIRE PERMISSION!\033[0m")
    print()
    print("  [press any key to continue] ", end="", flush=True)
    try:
        import termios, tty
        fd = sys.stdin.fileno()
        old = termios.tcgetattr(fd)
        try:
            tty.setcbreak(fd)        # cbreak keeps Ctrl+C working — raw would swallow it
            sys.stdin.read(1)
        finally:
            termios.tcsetattr(fd, termios.TCSADRAIN, old)
    except (ImportError, OSError):   # non-Linux/macOS or no tty → fallback requires Enter
        input()
    print()


# === Per-instance MEMORY.md reconcile (modifier-keyed addendums + seek_summary) ===
# Each modifier optionally has a `memory/<filename_form>-addendum.md` template
# that sync_memory_templates splices into per-instance MEMORY.md when the
# modifier is active, and removes when it's not. seek_summary.md is the only
# always-active template; it's project-wide rather than modifier-conditional.
# Template files hold content only — start/end wrapper marker lines are
# generated at sync time from the filename via _wrap_block, so the marker
# convention lives in code (one source of truth) rather than spread across
# template files.

def modifier_memory_templates(sess_id):
    """Build `(filename_form, active)` pairs for each modifier that has a
    matching `<filename_form>-addendum.md` template on disk. Iterates ALL
    `InstanceModifiers` (both tags and modes) so we also emit `active=False`
    entries for currently-inactive modes — those drive cleanup of orphaned
    blocks left behind after a previous launch removed the mode. Tags can't
    be turned off (they're filename-derived and immutable for the instance's
    lifetime), so for tags `active` is always True; the unified loop keeps
    the iteration shape consistent. Modifiers without an addendum template
    file are skipped here so the splicing loop doesn't need to recheck."""
    active = set(sess_id.tags) | set(sess_id.modes)
    return [(m.filename_form, m.value in active)
            for m in InstanceModifiers
            if path_exists(memory_template_path(m.filename_form))]


def _wrap_block(name, content):
    """Wrap `content` with banner-decorated `<name>-instructions-{start,end}`
    marker lines. The banner is purely a visual cue that the line is a
    machine-managed comment, not memory instructions for the agent. Single
    source of truth for the marker format — every wrapped block in MEMORY.md
    comes through here, and splice_block's marker detection keys on these
    exact lines."""
    banner = MEMORY_BLOCK_WRAPPER_BANNER
    return (f"{banner} {name}-instructions-start {banner}\n"
            f"{content.strip()}\n"
            f"{banner} {name}-instructions-end {banner}")


def sync_memory_templates(sess_id):
    """Reconcile per-instance MEMORY.md with the current modifier set. One
    read + at most one write per launch. Always-active seek_summary first,
    then each modifier addendum (active → spliced in / refreshed; inactive →
    spliced out). The [prog] addendum gets the installed cred-CLI names
    appended inline so the agent learns which credentialed tools are ready
    to use this launch. Content outside the wrapped blocks is preserved
    verbatim — that's where agent-added auto-memory pointer entries live.

    No defensive cleanup of unusual file types at memory_path — if read or
    write fails on a malformed state dir the caller can just delete the
    instance and create a new one."""
    memory_path = state_memory_path(sess_id.state_dir)
    original = read_text(memory_path) if path_exists(memory_path) else ""
    content = splice_block(original, _wrap_block("seek_summary", read_text(SEEK_SUMMARY_PATH)), keep=True)

    for modifier, active in modifier_memory_templates(sess_id):
        text = read_text(memory_template_path(modifier))
        if modifier == InstanceModifiers.TAG_PROG.filename_form:
            if clis := installed_cred_clis():
                text = text.rstrip() + " " + clis
            else:
                active = False   # no creds → nothing to advertise; drop the block
        content = splice_block(content, _wrap_block(modifier, text), keep=active)

    if content != original:
        write_text(memory_path, content)


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
