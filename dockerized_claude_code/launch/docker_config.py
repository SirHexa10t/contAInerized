"""Docker-side launcher orchestration — everything between "we picked an agent"
and `docker run`. The image-build chain (ensure_image), the bind-mount
accumulator that flattens into `-v` flags (set_container_mounts +
add_docker_mount + mount_target_is_staged), small `docker` CLI wrappers
(require_docker, detect_docker_gid, docker_check_running_subprocess,
wait_for_container_running, docker_exec_root_subprocess,
docker_check_any_agent_running_subprocess, docker_subprocess), the
image-naming helper (image_tag), the tag.docker flag emitters
(build_arg_flags / env_forward_flags / entrypoint_flags), the post-build
install-failure surfacing (prompt_install_failures), and the container
invocation itself (run_container).

Plain `docker build` / `docker run` throughout — the compose layer is retired.
Static per-tag container config (build-arg names, cap_add, entrypoint, mounts,
env forwards) is declared in each tag's `tag.docker` and arrives here as the
Instance's DockerContribution records; dynamic values (resolved firewall
addresses, detected DOCKER_GID) are staged into container_env by the
tag handlers and pulled by name at flag-emission time.

Sister accumulator lives in container_env: `_container_env` for env-var
staging. This module holds:
  - _docker_mounts: {source: "target[:ro]"} — staged via add_docker_mount,
    flattened inline by run_container into `-v` flags.

Imports from paths (filesystem constants), claude_code_config (terminal title),
container_env (env staging + flag formatters), and network (the {firewall}
coordination hooks). tag_handlers imports add_docker_mount +
docker_check_any_agent_running_subprocess + detect_docker_gid from here;
run.py is the top-level consumer.
"""

import fnmatch
import shutil
import subprocess
import sys
import time
from pathlib import Path

from .claude_code_config import set_terminal_title
from .container_env import (
    ContainerEnvKey, conf_env_args, container_env_args, stage_container_env,
    staged_env,
)
from .firewall import (
    is_critical_pending, selftest_address, start_firewall_updater,
    wait_for_critical_addresses,
)
from .paths import (
    BASE_DOCKERFILE, CLAUDE_CONFIG_IN_CONTAINER, DEFAULT_WORKSPACE,
    DOCKER_BASE_MOUNTS, DOCKERIZED_CLAUDE_ROOT, FIREWALL_DONE_IN_CONTAINER,
    INSTALL_FAILURES_LOG_IN_CONTAINER, LOCAL_BIN_IN_CONTAINER, RO_MOUNT_OPTION,
    state_settings_path,
)
from .tags import DockerContribution, Instance
from .template_code.docker_prompts import (
    BUILDING_STEP, FIREWALL_WAITING, INSTALL_FAILURES_BODY, INSTALL_FAILURES_HEADER,
)
from .utils import call_or_exit, exit_if_missing, prompt_keypress, shell_capture, shell_returncode


# ============================================================
# Docker volume accumulator
# ============================================================
# Every bind-mount for `docker run` flows through this dict. set_container_mounts
# stages the always-on set (paths.DOCKER_BASE_MOUNTS + the per-instance
# workspace/state dirs); tag_handlers stages each active tag's declarative
# `tag.docker` mounts plus the [code] cache mounts; user_additions stages
# skills + optional creds. Mirror of container_env's `_container_env` /
# stage_container_env pattern — declarations flow one way, emission stays in
# this module.

_docker_mounts: dict[str, str] = {}   # {source_path_str: "target_path[:ro]"} — source uniquely identifies a mount across our callers


def add_docker_mount(source: Path | str, target: Path | str) -> None:
    """Stage a bind-mount for the upcoming `docker run` invocation. Any
    docker access-mode suffix (`:ro`, also `:z`/`:Z`, `:cached`/`:delegated`,
    propagation modes) is the caller's responsibility — bake it into target
    when needed. Both args coerce to str at this boundary so callers can pass
    Path objects without thinking about it.

    Re-staging an identical (source, target) pair is an idempotent no-op.
    A *conflicting* duplicate raises RuntimeError: the same target from a
    different source would emit two `-v` flags docker rejects at run time
    (or the accumulator's source-keying would silently drop one mount for
    same-source/new-target) — better a clean launcher error at staging time
    than a cryptic docker one later. User-reachable clashes (`home/`
    contents-mounts) are pre-checked with a friendlier message in
    user_additions before ever reaching this guard."""
    src, tgt = str(source), str(target)
    staged = _docker_mounts.get(src)
    if staged is not None and staged != tgt:
        raise RuntimeError(f"bind-mount source {src} is already staged at {staged}; refusing to re-stage it at {tgt}")
    bare_target = tgt.split(":", 1)[0]
    if any(v.split(":", 1)[0] == bare_target and s != src for s, v in _docker_mounts.items()):
        raise RuntimeError(f"bind-mount target {bare_target} is already staged from a different source; refusing to shadow it with {src}")
    _docker_mounts[src] = tgt


def mount_target_is_staged(target: Path | str) -> bool:
    """True if any prior `add_docker_mount` call has already staged a mount at
    the given target (the access-mode suffix on the staged value, if any, is
    ignored). Used by overlay callers like `user_additions.home_overlay_mounts`
    to refuse to shadow a launcher-owned mount."""
    target_str = str(target)
    return any(v.split(":", 1)[0] == target_str for v in _docker_mounts.values())


# ============================================================
# Image naming
# ============================================================

def image_tag(layer_names: list[str]) -> str:
    """The docker image tag for a build-layer sequence. [] → 'claude-agents:base'.
    ['code', 'dood'] → 'claude-agents:code.dood'. Layer names are already
    lowercase (tag names are folder names)."""
    if not layer_names:
        return "claude-agents:base"
    return "claude-agents:" + ".".join(layer_names)


# ============================================================
# Docker subprocess helpers
# ============================================================
# Every docker-CLI touchpoint outside orchestration lives here: the PATH
# presence check (require_docker), the read-only probes used by firewall
# coordination + cache pruning (detect_docker_gid,
# docker_check_running_subprocess, wait_for_container_running,
# docker_exec_root_subprocess, docker_check_any_agent_running_subprocess),
# and the `docker` invocation wrapper (docker_subprocess) used by
# ensure_image / run_container below in the orchestration section.
# CONTAINER_NAME_PREFIX is the one place the per-launch container name format
# is defined — run_container builds container names from it, and
# docker_check_any_agent_running_subprocess filters `docker ps` by the same
# prefix; keeping them consistent is a one-line change here.

CONTAINER_NAME_PREFIX = "claude-code_"   # prefix for every per-launch container name (run_container) and the filter used to detect a running agent (docker_check_any_agent_running_subprocess)


# ============================================================
# Dry-run flag
# ============================================================
# Module-level toggle gating docker_subprocess's actual subprocess invocation.
# Set once at startup from run.py:launch via set_dry_run(); the default False
# means "real run" so callers that import this module without going through
# launch() (tests, audit) behave normally. The flag lives here rather than
# threaded through every function because the only operation it affects is
# the docker call itself — every other orchestration step (mount staging,
# env staging, firewall coordination, banner printing) happens identically
# in both modes, which is what makes --dry-run a faithful projection of a
# real run.

_dry_run = False


def set_dry_run(value: bool) -> None:
    """Set the module-level dry-run flag. Called from run.py:launch after CLI
    parsing. docker_subprocess checks this to gate its underlying
    subprocess.call — every surrounding step still runs so the user sees an
    accurate projection of what a real run would do."""
    global _dry_run
    _dry_run = value


def require_docker() -> None:
    """Exit early with a clean message if `docker` isn't on PATH. Run.py calls this
    at startup so a missing daemon surfaces as a one-liner instead of a deeper-down
    docker traceback later."""
    exit_if_missing(shutil.which("docker"), "docker is required but was not found in PATH.")


def detect_docker_gid() -> str | None:
    """Return the host's docker group GID as a string, or None if no docker
    group exists (or `getent` is unavailable — e.g. non-Linux hosts). Used by
    tag_handlers._apply_dood to stage DOCKER_GID for the `_dood` layer's
    Dockerfile, so claude can read/write the bind-mounted /var/run/docker.sock."""
    try:
        result = shell_capture("getent", "group", "docker")
    except FileNotFoundError:
        return None  # no getent (e.g., not Linux)
    if result.returncode == 0 and (out := result.stdout.strip()):
        return out.split(":")[2]
    return None


def docker_check_running_subprocess(container_name: str) -> bool:
    """True if the named container is currently in the Running state per
    `docker inspect`. False otherwise — returncode non-zero (container not
    found / daemon unreachable), or `State.Running` is anything other than
    the literal string `"true"` (docker's text output for that field).
    One-shot probe; wait_for_container_running polls this in a loop for the
    "just-created, not yet up" window."""
    r = shell_capture("docker", "inspect", "--format={{.State.Running}}", container_name)
    return r.returncode == 0 and r.stdout.strip() == "true"


def wait_for_container_running(container_name: str, timeout_seconds: float = 10) -> bool:
    """Poll `docker_check_running_subprocess` until it returns True, or
    `timeout_seconds` passes. Returns True if the container came up in time,
    False on timeout. `docker run` creates the container almost immediately
    but `docker inspect` returns 'not found' for a small window after —
    hence the poll. Used by the {firewall} updater (in firewall.resolver._updater_worker)
    before it starts issuing `docker exec` calls.

    The walrus in the while-condition reads as "while within deadline and not
    yet running, sleep". `running = False` is initialized to keep the name
    bound for the return even when the walrus never fires (deadline already
    passed on entry — `timeout_seconds <= 0`)."""
    deadline = time.monotonic() + timeout_seconds
    running = False
    while time.monotonic() < deadline and not (running := docker_check_running_subprocess(container_name)):
        time.sleep(0.1)
    return running


def wait_for_firewall_applied(container_name: str, timeout_seconds: float = 90) -> bool:
    """Gate for the phase-2 firewall updater: True when it's sensible to
    start inserting rules, False when there's nothing left to update.

    Polls for init-firewall.sh's completion marker
    (paths.FIREWALL_DONE_IN_CONTAINER). Marker present → True. Container
    stopped without it → False (init-firewall failed its self-test and took
    the container down). Deadline passed with the container still up → True
    anyway, best-effort: the script's runtime is curl-bounded to seconds, so
    a live container without a marker after this long means the marker
    mechanism itself broke — and late rules beat no rules.

    The gate exists because "container is running" is NOT "firewall is
    ready": the entrypoint runs init-firewall.sh as its first act, so an
    updater that starts inserting rules on mere running-ness races the
    script — inserts landing before its `iptables -F` were silently wiped,
    and inserts landing mid-self-test could open provider blocks that made
    the enforcement probe's target reachable, killing perfectly healthy
    launches. Used by firewall.resolver._updater_worker between
    wait_for_container_running and the first rule flush."""
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        if docker_exec_root_subprocess(container_name, "test", "-e", str(FIREWALL_DONE_IN_CONTAINER)).returncode == 0:
            return True
        if not docker_check_running_subprocess(container_name):
            return False
        time.sleep(0.3)
    return docker_check_running_subprocess(container_name)


def docker_exec_root_subprocess(container_name: str, *cmd: str) -> subprocess.CompletedProcess:
    """Run `docker exec --user root <container_name> <cmd...>` and return the
    CompletedProcess (capture_output=True, text=True so callers can inspect
    returncode + stdout/stderr).

    The `--user root` flag is the privileged-operation pattern: it grants
    root inside the container *from outside the container's namespace*,
    bypassing whatever sudoers restrictions are in place for the in-container
    user. Used by the {firewall} updater (in firewall.resolver._flush_rules)
    to inject iptables ACCEPT rules into the running container as Phase 2 DNS
    resolutions complete. Centralised here so privileged docker-exec calls
    have a single audit point."""
    return shell_capture("docker", "exec", "--user", "root", container_name, *cmd)


def docker_subprocess(args: list[str]) -> None:
    """Run `docker <args>`; sys.exit with the return code on non-zero, return
    silently on success. On dry-run (set via set_dry_run), print what would
    have been invoked and return without touching subprocess — every
    surrounding orchestration step still runs, so dry-run projects accurately.

    Both real-run callers want the exit-on-failure shape: ensure_image needs
    to continue to the next chain step on success but die if any build fails;
    run_container is the program's terminal — on success the unwind through
    launch() → __main__ exits the process with 0 naturally, equivalent to an
    explicit sys.exit(0)."""
    if _dry_run:
        print(f"  (dry-run: would invoke `docker {' '.join(args)}`)")
        return
    if (ret := shell_returncode("docker", *args)) != 0:
        sys.exit(ret)


def docker_check_any_agent_running_subprocess() -> bool:
    """True if any container whose name starts with CONTAINER_NAME_PREFIX is
    currently running, OR if `docker ps` failed (conservative — treat the
    unknown state as 'might be running' so caller skips its cleanup). Used
    by tag_handlers.prune_caches as the 'is it safe to delete cache files'
    guard. Uses `bool(stdout.strip())` rather than `== "true"` because
    `--format={{.Names}}` outputs container names (one per line) — any
    non-empty output means matching containers exist."""
    r = shell_capture("docker", "ps", "--filter", f"name={CONTAINER_NAME_PREFIX}", "--format", "{{.Names}}")
    return r.returncode != 0 or bool(r.stdout.strip())


# ============================================================
# tag.docker flag emitters
# ============================================================

def build_arg_flags(forward: tuple[str, ...]) -> list[str]:
    """`--build-arg NAME=VALUE` flags for a layer's `[build] arg_forward`
    list, values pulled from the staged container env. Glob patterns expand
    against the staged keys (`INSTALL_*` → every staged INSTALL_<TOOL>);
    a plain name that isn't staged is silently skipped — the Dockerfile's
    own ARG default then applies (that's how DOCKER_GID keeps its 999
    fallback when detection is bypassed)."""
    staged = staged_env()
    out: list[str] = []
    for pattern in forward:
        if any(ch in pattern for ch in "*?["):
            keys = sorted(fnmatch.filter(staged, pattern))
        else:
            keys = [pattern] if pattern in staged else []
        out += [arg for k in keys for arg in ("--build-arg", f"{k}={staged[k]}")]
    return out


def env_forward_flags(contributions: list[DockerContribution]) -> list[str]:
    """`-e NAME=VALUE` flags for every `[run] env_forward` name across the
    active tags' contributions, values pulled from the staged container env.
    Unstaged names are silently skipped — that's the gating: {firewall}'s
    WHITELIST_ADDRESSES is only staged when the resolve actually ran."""
    staged = staged_env()
    names = [n for c in contributions for n in c.env_forward]
    return [arg for n in names if n in staged for arg in ("-e", f"{n}={staged[n]}")]


def entrypoint_flags(contributions: list[DockerContribution]) -> list[str]:
    """The `--entrypoint <path>` flag if exactly one active tag overrides the
    entrypoint; [] when none does (the image's own ENTRYPOINT — claude —
    applies). A bare script name resolves to LOCAL_BIN_IN_CONTAINER (where
    the owning tag's mounts put it); a path is used as-is. Two tags both
    claiming the entrypoint can't compose — fail loud."""
    entrypoints = [c.entrypoint for c in contributions if c.entrypoint]
    if not entrypoints:
        return []
    if len(entrypoints) > 1:
        raise RuntimeError(f"multiple active tags override the container entrypoint: {entrypoints}")
    ep = entrypoints[0]
    resolved = ep if "/" in ep else f"{LOCAL_BIN_IN_CONTAINER}/{ep}"
    return ["--entrypoint", resolved]


# ============================================================
# Orchestration
# ============================================================

def set_container_mounts(inst_id: Instance) -> None:
    """Stage per-launch bind-mounts via add_docker_mount. Sister to set_container_env
    (bind-mounts vs env vars); both run sequentially in setup_state. Two layers:
    the per-instance pair (workspace → /workspace, state dir → /home/claude/.claude)
    derived from inst_id, plus the always-on DOCKER_BASE_MOUNTS from paths.py
    (whose target strings already carry any `:ro` suffix).

    The launcher-generated settings file (base settings + policy fragments,
    written by agents_crud.install_settings) mounts READ-ONLY over
    `~/.claude/settings.json` — the file mount shadows the state-dir's rw
    view of the same path, so the agent can't relax its own policies.

    Workspace fallback: if `inst_id.workspace` is None (stale store entry that
    survived all the upstream prompts, or a session constructed without a
    workspace), default to DEFAULT_WORKSPACE so the bind-mount still resolves
    to a real host directory rather than crashing the docker invocation.

    Workspace access mode: read-only when a `workspace_readonly` specialty
    (e.g. `{frozen}`) is active — the mount itself denies writes, a harder
    guarantee than the `read-only` policy's harness-level tool denial. The
    state dir stays read-write regardless (Claude Code writes history/memory
    there)."""
    workspace_target = "/workspace" + (f":{RO_MOUNT_OPTION}" if inst_id.workspace_readonly else "")
    add_docker_mount(inst_id.workspace or DEFAULT_WORKSPACE, workspace_target)
    add_docker_mount(inst_id.state_dir, CLAUDE_CONFIG_IN_CONTAINER)
    add_docker_mount(state_settings_path(inst_id.state_dir),
                     f"{CLAUDE_CONFIG_IN_CONTAINER}/settings.json:{RO_MOUNT_OPTION}")
    for source, target in DOCKER_BASE_MOUNTS.items():
        add_docker_mount(source, target)


def ensure_image(inst: Instance) -> str:
    """Build the instance's image stack bottom-up with plain `docker build`
    and return the final image tag. The base image builds from
    paths.BASE_DOCKERFILE; each subsequent step comes from the instance's
    build_steps (profession Dockerfiles + layer-bearing specialties), with
    PARENT_IMAGE pointing at the prior step's tag so each Dockerfile's
    `FROM ${PARENT_IMAGE}` resolves to a freshly-built parent. Each step
    forwards only the build-args its own `tag.docker` names (values from the
    staged container env), so one layer's args never surface in another's
    build. `--network=host` because BuildKit's own bridge has had DNS
    failures resolving curl/apt repo hosts on some setups. The build context
    is always the repo root."""
    tag = image_tag([])
    print(BUILDING_STEP.format(step="base", target=tag))
    docker_subprocess([
        "build", "--network=host", "-f", str(BASE_DOCKERFILE), "-t", tag,
        *build_arg_flags((str(ContainerEnvKey.SOFTWARE_STACK_REFRESH),)),
        str(DOCKERIZED_CLAUDE_ROOT),
    ])
    names: list[str] = []
    for name, dockerfile, contribution in inst.build_steps:
        parent = tag
        names.append(name)
        tag = image_tag(names)
        print(BUILDING_STEP.format(step=name, target=tag))
        docker_subprocess([
            "build", "--network=host", "-f", str(dockerfile), "-t", tag,
            "--build-arg", f"PARENT_IMAGE={parent}",
            *build_arg_flags(contribution.build_arg_forward if contribution else ()),
            str(DOCKERIZED_CLAUDE_ROOT),
        ])
    return tag


def prompt_install_failures(image: str, instance: str) -> None:
    """Read INSTALL_FAILURES_LOG_IN_CONTAINER from the final image; if it's
    non-empty, surface the failed tool names as a press-any-key prompt (so
    Claude Code's TUI takeover doesn't immediately clobber the warning).
    No-op when the file is missing (no [code] step in the stack) or empty
    (all installs succeeded). Self-contained: the prompt copy lives in
    template_code/docker_prompts and the rendering goes through prompt_keypress;
    nothing flows back to the caller. Uses `docker run --rm --entrypoint cat`
    for the one-shot read — one extra subprocess per launch (~few hundred ms
    after a warm image cache). Called by run.py between ensure_image and
    run_container so the list reflects the build that just finished.

    No-op on dry-run: ensure_image built nothing, so the only readable log
    would be a stale one from a previous real build — spinning up a real
    container to surface stale warnings would break dry-run's "project,
    don't touch" contract."""
    if _dry_run:
        return
    result = shell_capture(
        "docker", "run", "--rm", "--entrypoint", "cat",
        image, str(INSTALL_FAILURES_LOG_IN_CONTAINER),
    )
    if result.returncode != 0:
        return
    failures = sorted({line.strip() for line in result.stdout.splitlines() if line.strip()})
    if not failures:
        return
    prompt_keypress(
        header=INSTALL_FAILURES_HEADER.format(failures=", ".join(failures)),
        body=[line.format(instance=instance) for line in INSTALL_FAILURES_BODY],
    )


def effort_args(conf: dict[str, str], claude_args: list[str]) -> list[str]:
    """CLI args pinning the session's effort to the conf's
    CLAUDE_CODE_EFFORT_LEVEL (e.g. ["--effort", "max"]), or [] when the conf
    doesn't set a level or the user passed their own --effort through
    (theirs wins — both the `--effort max` and `--effort=max` forms count).

    Why a CLI flag when the same value already ships as a -e env var: on
    newly-launched models (Opus 4.7/4.8, Fable 5) Claude Code pins a fresh
    interactive session's effort to the model's launch default ("high") and
    treats CLAUDE_CODE_EFFORT_LEVEL as a session-only override it neither
    persists nor reflects in the /model UI — its pin logic explicitly checks
    argv for --effort as the user's confirmation. Passing the documented
    flag is the supported way to declare the level so the session both runs
    at it and reports it."""
    effort = conf.get("CLAUDE_CODE_EFFORT_LEVEL")
    if not effort or any(a == "--effort" or a.startswith("--effort=") for a in claude_args):
        return []
    return ["--effort", effort]


def run_container(inst: Instance, image: str, claude_args: list[str], resume_flag: list[str]) -> None:
    """Assemble and exec the final `docker run`. By the time we get here
    every bind-mount has been staged via add_docker_mount (base set,
    per-instance workspace/state, tag.docker mounts, [code] caches, skills,
    optional creds) — flatten _docker_mounts into `-v` flags inline. The
    active tags' contributions supply cap_add / entrypoint / env forwards;
    the engine conf and specialty claude_args ride the Instance. On a
    non-zero container return, docker_subprocess sys.exits with that code;
    on zero, returns normally and the __main__ unwind exits 0. The image
    itself is built upstream by ensure_image (called from run.py:launch
    before this).

    {firewall} coordination: block on Phase 1 (critical Anthropic DNS) to
    get the initial WHITELIST_ADDRESSES — exiting with the worker's one-line
    message if a critical domain terminally failed to resolve — then spawn
    the firewall updater daemon thread BEFORE `docker run` so it can drain
    Phase 2 results into the running container's iptables via `docker exec`
    while Claude Code starts up. Both steps no-op when {firewall} is off
    (no resolution was started). `--name` is set explicitly to a
    deterministic string so the updater knows where to point.

    `-e TERM` is passed bare — docker forwards the value from the launcher's
    own terminal environment."""
    contributions = inst.docker_contributions
    set_terminal_title(inst.instance)
    # Phase 1 await: block for critical Anthropic addresses, stage them as the
    # initial WHITELIST_ADDRESSES. Phase 2 (rest of the whitelist) drains via
    # the updater thread spawned below.
    if is_critical_pending():
        print(FIREWALL_WAITING, flush=True)
    # A terminally-failed critical resolve surfaces as the phase-1 worker's
    # RuntimeError — exit with its message, not a raw traceback.
    addresses = call_or_exit(wait_for_critical_addresses, exceptions=RuntimeError)
    if addresses is not None:
        stage_container_env(ContainerEnvKey.WHITELIST_ADDRESSES, " ".join(addresses))
        if (selftest := selftest_address()) is not None:
            stage_container_env(ContainerEnvKey.FIREWALL_SELFTEST_ADDR, selftest)
    container_name = f"{CONTAINER_NAME_PREFIX}{inst.instance}"
    # Spawn the updater BEFORE docker_subprocess (which blocks for the
    # container's lifetime). No-op for non-{firewall} launches.
    start_firewall_updater(container_name)
    args = (
        ["run", "--rm", "-it", "--name", container_name, "-e", "TERM"]
        + [f"--cap-add={cap}" for c in contributions for cap in c.cap_add]
        + entrypoint_flags(contributions)
        + [arg for src, tgt in _docker_mounts.items() for arg in ("-v", f"{src}:{tgt}")]
        + container_env_args()             # always-on -e flags (status line, BASH_ENV, cred tokens)
        + conf_env_args(inst.conf)         # -e flags setting each engine-conf key=value in the container
        + env_forward_flags(contributions)  # tag-conditional -e flags (WHITELIST_ADDRESSES)
        + [image]
        + effort_args(inst.conf, claude_args)  # explicit --effort from the conf — see effort_args for why the env var isn't enough
        + resume_flag                      # present if a resumed session
        + list(inst.claude_args)           # specialty-contributed flags ({auto}'s --dangerously-skip-permissions)
        + claude_args                      # leftover argv (unrecognised flags + unresolved positional) → claude
    )
    docker_subprocess(args)
