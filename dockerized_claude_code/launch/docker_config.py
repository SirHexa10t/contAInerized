"""Docker-side launcher orchestration — everything between "we picked an agent"
and `docker run`. The image-build chain (ensure_image), the bind-mount
accumulator that flattens into `-v` flags (set_container_mounts +
add_docker_mount + mount_target_is_staged), small `docker` CLI wrappers
(require_docker, detect_docker_gid, docker_subprocess,
docker_running_instances_subprocess,
docker_check_any_agent_running_subprocess), the
image-naming helper (image_tag), the tag.docker flag emitters
(build_arg_flags / env_forward_flags / entrypoint_chain), the post-build
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
  - _docker_mounts: [(source, "target[:ro]"), ...] — staged via
    add_docker_mount by BOTH launch shapes, flattened inline by run_container
    into `-v` flags, or snapshotted (`staged_mounts`) for the cluster run.

Imports from paths (filesystem constants), claude_code_config (terminal
title), container_env (env staging + flag formatters), container_probe (the
container-name format) and firewall (the {firewall} coordination hooks).
tag_handlers imports add_docker_mount +
docker_check_any_agent_running_subprocess + detect_docker_gid from here;
run.py is the top-level consumer.
"""

import fnmatch
import shutil
import subprocess
import sys
from collections.abc import Callable, Iterable
from pathlib import Path

from .ai import Adapter, active_adapter
from .claude_code_config import credentials_notice, set_terminal_title
from .container_env import (
    ContainerEnvKey, conf_env_args, container_env_args, stage_container_env,
    staged_env,
)
from .container_probe import CONTAINER_NAME_PREFIX
from .file_access import ensure_auth_files, ensure_dir, home_relative, is_file, key_file_problems, make_private
from .firewall import (
    is_critical_pending, selftest_address, start_firewall_updater,
    wait_for_critical_addresses,
)
from .paths import (
    BASE_DOCKERFILE, CLAUDE_CONFIG_IN_CONTAINER, cowork_dir_path, COWORK_IN_CONTAINER,
    DEFAULT_WORKSPACE, DOCKERIZED_CLAUDE_ROOT,
    INSTALL_FAILURES_LOG_IN_CONTAINER, LOCAL_BIN_IN_CONTAINER, RO_MOUNT_OPTION,
    base_mounts, key_file,
)
from .tags import Ai, DockerContribution, Instance
from .template_code.docker_prompts import (
    BUILDING_STEP, FIREWALL_WAITING, INSTALL_FAILURES_BODY, INSTALL_FAILURES_HEADER,
)
from .utils import (
    call_or_exit, exit_if_missing, prompt_keypress, reset_terminal,
    shell_capture, shell_returncode,
)


# ============================================================
# Docker volume accumulator
# ============================================================
# Every bind-mount for `docker run` flows through this dict. set_container_mounts
# stages the always-on set (paths.DOCKER_BASE_MOUNTS + the per-instance
# workspace/state dirs, plus the {cowork} group-hosting dir when that tag is
# active); tag_handlers stages each active tag's declarative
# `tag.docker` mounts plus the [code] cache mounts; user_additions stages
# skills + optional creds. Mirror of container_env's `_container_env` /
# stage_container_env pattern — declarations flow one way, emission stays in
# this module.

# `(source, "target[:ro]")` PAIRS in staging order — not a source-keyed dict:
# one host file legitimately mounts at several targets (a cluster binds the
# shared login file into every member's config dir), and the dict that held
# this until 2026-09-15 silently kept only the last such mount, which is why
# the cluster grew its own list and its own (unchecked) merge. One
# accumulator, one collision rule, both launch shapes.
_docker_mounts: list[tuple[str, str]] = []


def add_docker_mount(source: Path | str, target: Path | str) -> None:
    """Stage a bind-mount for the upcoming `docker run` invocation — the ONE
    place either launch shape adds one, so the collision rule below holds
    for a solo instance and a cluster alike. Any docker access-mode suffix
    (`:ro`, also `:z`/`:Z`, `:cached`/`:delegated`, propagation modes) is the
    caller's responsibility — bake it into target when needed. Both args
    coerce to str at this boundary so callers can pass Path objects without
    thinking about it.

    Re-staging an identical (source, target) pair is an idempotent no-op. A
    second mount at the same container path — from another source, or the
    same source with another access mode — raises RuntimeError: two `-v`
    flags for one target make docker fail at run time with a message naming
    neither culprit, so better a clean launcher error at staging time. The
    same SOURCE at a new target is legal, and needed (see `_docker_mounts`).
    User-reachable clashes (`home/` contents-mounts) are pre-checked with a
    friendlier message in user_additions before ever reaching this guard."""
    src, tgt = str(source), str(target)
    if (src, tgt) in _docker_mounts:
        return
    bare_target = tgt.split(":", 1)[0]
    for staged_src, staged_tgt in _docker_mounts:
        if staged_tgt.split(":", 1)[0] == bare_target:
            raise RuntimeError(f"bind-mount target {bare_target} is already staged from {staged_src}; refusing to shadow it with {src}")
    _docker_mounts.append((src, tgt))


def staged_mounts() -> tuple[tuple[str, str], ...]:
    """A snapshot of every staged pair, in staging order — what a cluster
    hands `run_cluster_container` (held on its PreparedLaunch so the CLI can
    show it and tests can assert on it); the solo run reads the accumulator
    itself."""
    return tuple(_docker_mounts)


def mount_target_is_staged(target: Path | str) -> bool:
    """True if any prior `add_docker_mount` call has already staged a mount at
    the given target (the access-mode suffix on the staged value, if any, is
    ignored). Used by overlay callers like `user_additions.home_overlay_mounts`
    to refuse to shadow a launcher-owned mount."""
    target_str = str(target)
    return any(tgt.split(":", 1)[0] == target_str for _, tgt in _docker_mounts)


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
# Every FLEET-level docker-CLI touchpoint lives here: the PATH presence check
# (require_docker), the host/daemon probe (detect_docker_gid), the "what is
# running right now" queries (docker_running_instances_subprocess,
# docker_check_any_agent_running_subprocess),
# and the `docker` invocation wrapper (docker_subprocess) used by
# ensure_image / run_container below in the orchestration section.
# Talking to ONE named, live container (probe it, wait for it, read its tty
# size, exec in it) is `container_probe`'s job instead, and writing to one is
# `container_inject`'s — that split is what keeps the firewall subsystem and
# the group-hosting layer importable without this module.
# CONTAINER_NAME_PREFIX is imported rather than defined here: it is still the
# one definition of the per-launch name format, but it lives with the calls
# that ADDRESS a container, and this module is one of its consumers —
# run_container builds names from it, docker_running_instances_subprocess
# filters `docker ps` by it and strips it back off to recover instance ids.


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
    """Exit early with a clean, verbose message when docker isn't usable — either
    not on PATH, or installed but the daemon isn't responding. On the daemon
    case the message carries the client version (and flags the server as
    unreachable) so a docker problem surfaces with concrete version numbers here
    at startup, instead of a deeper docker traceback mid-launch. Called on every
    launch (dry-run included — a faithful projection needs a live daemon just as
    a real run does)."""
    exit_if_missing(
        shutil.which("docker"),
        "docker is required but was not found in PATH. Install Docker "
        "(Desktop on macOS, Engine >= 20.10 on Linux; see README) and retry.",
    )
    probe = shell_capture("docker", "version")
    if probe.returncode != 0:
        detail = (probe.stderr or probe.stdout).strip().splitlines()
        sys.exit(
            "docker is installed but not responding — the daemon looks down.\n"
            f"  client version: {_docker_client_version() or 'unknown'}\n"
            "  server version: unreachable (is Docker Desktop / the daemon running?)\n"
            f"  docker said: {detail[-1] if detail else 'docker version exited non-zero'}\n"
            "Start Docker and retry."
        )


def _docker_client_version() -> str | None:
    """The docker CLIENT version string (readable without a running daemon), or
    None if it can't be determined."""
    r = shell_capture("docker", "version", "--format", "{{.Client.Version}}")
    return r.stdout.strip() if r.returncode == 0 and r.stdout.strip() else None


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


def _interactive_docker_run(args: list[str]) -> None:
    """docker_subprocess for the terminal-OWNING runs (solo `-it`, cluster),
    with the tty repaired on the way out — crucially BEFORE the nonzero-rc
    exit, because a container that died out from under this terminal
    (`--stop` from another session, a crash, `herdr server stop`) IS the
    nonzero case: docker restores the tty's termios but knows nothing of the
    escape-sequence modes the in-container TUI set on the EMULATOR (mouse
    tracking, alt screen, hidden cursor). Left set, every mouse move echoes
    as endless `35;77;15M` garbage at the shell. The repair also drains
    already-queued mouse reports; on a healthy teardown it is a handful of
    idempotent no-op sequences."""
    if _dry_run:
        docker_subprocess(args)     # prints the would-run line, nothing to repair
        return
    ret = shell_returncode("docker", *args)
    reset_terminal(drain_input=True)
    if ret != 0:
        sys.exit(ret)


def docker_stream_subprocess(args: list[str], on_lines: Callable[[Iterable[str]], None]) -> None:
    """Run `docker <args>` with stdout piped line-by-line into `on_lines`
    (quickie's stream-json renderer). stderr inherits the terminal; stdin is
    /dev/null so a stream-mode `claude` doesn't stall waiting for input. Exits
    with the container's return code on failure, mirroring docker_subprocess;
    dry-run prints the would-be invocation and skips it (the renderer never
    runs, matching docker_subprocess's no-op)."""
    if _dry_run:
        print(f"  (dry-run: would invoke `docker {' '.join(args)}`)")
        return
    with subprocess.Popen(["docker", *args], stdin=subprocess.DEVNULL,
                          stdout=subprocess.PIPE, text=True) as proc:
        assert proc.stdout is not None
        on_lines(proc.stdout)
    if proc.returncode:
        # Say so: a headless run that fails after the build output and exits
        # with a bare status code reads as "nothing happened" (2026-09-16).
        print(f"  [quickie] the container exited with status {proc.returncode}.", file=sys.stderr, flush=True)
        sys.exit(proc.returncode)


def docker_running_instances_subprocess() -> frozenset[str] | None:
    """The instance ids (`<agent>__<session>`) whose containers are running
    right now — one `docker ps` for all of them — or None when the state
    couldn't be determined (`docker ps` failed / daemon unreachable).

    None is a distinct third state, not an empty set, because the two callers
    want OPPOSITE failure behaviour: prune_caches must assume "something might
    be running" and skip deleting, while the picker must assume "mark nothing"
    rather than flag every row. Returning the unknown separately lets one
    docker call serve both.

    `--filter name=` is a substring/regex match against docker's own names
    (stored with a leading `/`), so it only NARROWS the listing cheaply — the
    authoritative check is `startswith(CONTAINER_NAME_PREFIX)` here, keeping
    CONTAINER_NAME_PREFIX the single source of truth for the name format and
    making the parse unit-testable without a daemon."""
    try:
        r = shell_capture("docker", "ps", "--filter", f"name={CONTAINER_NAME_PREFIX}", "--format", "{{.Names}}")
    except OSError:
        return None      # docker not on PATH at all — same "can't tell" as a failed probe
    if r.returncode != 0:
        return None
    return frozenset(name.removeprefix(CONTAINER_NAME_PREFIX)
                     for line in r.stdout.splitlines()
                     if (name := line.strip()).startswith(CONTAINER_NAME_PREFIX))


def docker_stop_subprocess(container_id: str, timeout_seconds: int = 3) -> bool:
    """`docker stop` one launcher container; True when docker reports success.
    Takes the PREFIX-STRIPPED id — the spelling of
    docker_running_instances_subprocess results and cluster_container_id, so
    the `--stop` flow never re-derives names. The short grace period is
    deliberate: a `{muxer}` entrypoint is a plain `sh` script that ignores
    SIGTERM, so stopping one always rides out the full timeout before docker
    KILLs — and containers run `--rm`, so stopped IS removed (state dirs and
    conversations live host-side and persist regardless).

    `-t`, on purpose: the long spelling is a version trap — newer docker
    deprecates `--time` with a warning (operator-observed, 2026-08-31) while
    its replacement `--timeout` is UNKNOWN to the Docker 20.10 floor
    install_dependencies.sh enforces. The short flag is the one spelling
    every supported docker accepts silently."""
    return shell_returncode("docker", "stop", "-t", str(timeout_seconds),
                            f"{CONTAINER_NAME_PREFIX}{container_id}") == 0


def docker_check_any_agent_running_subprocess() -> bool:
    """True if any agent container is currently running, OR if the running
    state couldn't be determined (conservative — treat the unknown as 'might
    be running' so the caller skips its cleanup). Used by
    tag_handlers.prune_caches as the 'is it safe to delete cache files' guard."""
    running = docker_running_instances_subprocess()
    return running is None or bool(running)


def cluster_container_id(session: str) -> str:
    """A cluster container's identity with CONTAINER_NAME_PREFIX stripped —
    the form a cluster takes in docker_running_instances_subprocess results
    (the probe strips the prefix and keeps the rest, whatever kind of launch
    named the container). One definition, so run_cluster_container's naming
    and every is-it-running check cannot drift. The `cluster-` infix is also
    what keeps the id disjoint from instance ids: those are
    `<agent>__<session>` and an agent name cannot contain `-<anything>`
    before the separator."""
    return f"cluster-{session}"


def running_cluster_report(session: str) -> str | None:
    """`running_instance_report`'s cluster twin: a refusal message when the
    cluster already has a live container, else None. Same rationale — one
    fresh probe at launch time covers a picker row whose running-snapshot
    went stale while the menu sat open, and turns docker's late duplicate
    `--name` error into an early, readable one."""
    running = docker_running_instances_subprocess()
    if running is None or cluster_container_id(session) not in running:
        return None
    return (f"  Cluster '{session}' is already running "
            f"(container {CONTAINER_NAME_PREFIX}{cluster_container_id(session)}).\n"
            f"  Stop that container, or switch to the terminal that's attached to it.")


def running_instance_report(inst: Instance) -> str | None:
    """A one-line refusal message when `inst` already has a live container,
    else None — same shape as agents_crud.invalid_tags_report so run.py's two
    launch guards read alike.

    One check covers every route to a launch: a CLI target, a picker row whose
    running-snapshot went stale while the menu sat open, and a brand-new
    session name that happens to collide with a live container. It runs on the
    fully-resolved identity before anything is persisted or built, because
    `docker run --name` would refuse the duplicate anyway — this turns that
    late, raw daemon error into an early, readable one. Re-probes rather than
    reusing the picker's snapshot: freshness at launch time is the whole point."""
    running = docker_running_instances_subprocess()
    if running is None or inst.instance not in running:
        return None
    return (f"  Instance '{inst.instance}' is already running "
            f"(container {CONTAINER_NAME_PREFIX}{inst.instance}).\n"
            f"  Stop that container, or switch to the terminal that's running it.")


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


def key_env_file_args(ai_keys: Iterable[str]) -> list[str]:
    """`--env-file <path>` for every AI among `ai_keys` whose key file
    (`credentials/keys/<ai>.env`) exists — the AI's API key(s) reach the
    container as environment without appearing on the docker command line.
    An AI without a key file contributes nothing: the harness then runs on its
    OAuth files, or asks to log in (plans/credentials.md)."""
    files = sorted({str(key_file(ai)) for ai in ai_keys if is_file(key_file(ai))})
    return [arg for path in files for arg in ("--env-file", path)]


def stage_credentials(adapter: Adapter, ais: Iterable[Ai]) -> list[str]:
    """The credentials staging every launch shape runs, once per (harness,
    AI) pair — a solo instance, the quickie, each cluster member: the
    harness's login files exist as private blanks before docker binds them
    (`ensure_auth_files`), each AI's key file passes `preflight_key_file`
    (RuntimeError propagates — the caller decides how it surfaces), and the
    notices worth printing before docker runs come back (`credentials_notice`).
    One function so the paths cannot drift: they did, on 2026-09-15."""
    ensure_auth_files(adapter)
    notices: list[str] = []
    for ai in ais:
        preflight_key_file(ai)
        if (notice := credentials_notice(adapter, ai)) is not None and notice not in notices:
            notices.append(notice)
    return notices


def preflight_key_file(ai: Ai) -> None:
    """Before any docker work: if the AI has a key file, fix its mode (600 —
    the ssh helper's precedent: fix, don't nag) and refuse a file docker would
    mis-read (`file_access.key_file_problems`), naming variables and lines
    but never a value. RuntimeError, like the optional-creds clash — the
    launch paths wrap it in call_or_exit."""
    path = key_file(ai.name)
    if not is_file(path):
        return
    make_private(path)
    problems = key_file_problems(path, ai.key_env)
    if problems:
        raise RuntimeError(f"{home_relative(path)} would not authenticate {ai.label} — fix it, or remove it to log in inside "
                           f"the container instead:\n  " + "\n  ".join(problems))


def env_forward_flags(contributions: list[DockerContribution]) -> list[str]:
    """`-e NAME=VALUE` flags for every `[run] env_forward` name across the
    active tags' contributions, values pulled from the staged container env.
    Unstaged names are silently skipped — that's the gating: {firewall}'s
    WHITELIST_ADDRESSES is only staged when the resolve actually ran."""
    staged = staged_env()
    names = [n for c in contributions for n in c.env_forward]
    return [arg for n in names if n in staged for arg in ("-e", f"{n}={staged[n]}")]


def entrypoint_chain(contributions: list[DockerContribution]) -> tuple[list[str], list[str]]:
    """`(flags, inner)` — the `--entrypoint` flag plus the links that follow it.

    Several tags can legitimately want to wrap the agent: `{firewall}` applies
    iptables first, `{muxer}` starts it inside a multiplexer. They compose as a
    CHAIN rather than competing: the first script becomes docker's entrypoint and
    every later one is handed to it as arguments, so each wrapper does its own job
    and then `exec "$@"`s the next. `{firewall}`'s script ends that way for
    exactly this reason (it used to `exec claude` and could not be followed).

    Order comes from `contributions`, which is already in chain order
    (`identity._ordered_groups`), and it matters: the firewall must be applied
    before anything else runs, and its `sudo -k` must happen before the agent
    starts. `test_docker_config` pins firewall-before-muxer so the ordering is a
    checked property rather than a coincidence of directory names.

    A bare script name resolves to LOCAL_BIN_IN_CONTAINER (where the owning tag's
    mounts put it); a path is used as-is. No overrides at all → `([], [])`, and the
    image's own ENTRYPOINT (the harness layer's binary) applies."""
    entrypoints = [c.entrypoint for c in contributions if c.entrypoint]
    if not entrypoints:
        return [], []
    resolved = [ep if "/" in ep else f"{LOCAL_BIN_IN_CONTAINER}/{ep}"
                for ep in entrypoints]
    return ["--entrypoint", resolved[0]], resolved[1:]


# ============================================================
# Orchestration
# ============================================================

def set_container_mounts(inst_id: Instance) -> None:
    """Stage a SOLO container's own bind-mounts via add_docker_mount. Sister
    to set_container_env (bind-mounts vs env vars); both run sequentially in
    setup_state. Two layers: the container's pair (workspace → /workspace,
    state dir → /home/claude/.claude) derived from inst_id, plus the
    always-on base set from paths.py (whose target strings already carry any
    `:ro` suffix). The agent's own files — settings and commands shadows, the
    harness's login files — are NOT staged here: `staging.stage_instance`
    stages those for every shape, solo or cluster member. A `{cowork}`
    instance additionally gets its group-hosting dir at COWORK_IN_CONTAINER —
    see below.

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
    if inst_id.is_cowork:
        # {cowork}: this instance's group-hosting dir, read-write. Created here
        # rather than by the hub because docker fixes mounts at container
        # creation — a missing source would be created by docker as a
        # root-owned dir the container's `claude` user could not write.
        # The `_cowork` Stop hook writes captures under this mount, which is
        # what makes them outlive the container.
        cowork_dir = cowork_dir_path(inst_id.instance)
        ensure_dir(cowork_dir)
        add_docker_mount(cowork_dir, str(COWORK_IN_CONTAINER))
    for source, target in base_mounts():
        add_docker_mount(source, target)


def ensure_image(inst: Instance, *, pull: bool = False) -> str:
    """Build the instance's image stack bottom-up with plain `docker build`
    and return the final image tag. The base image builds from
    paths.BASE_DOCKERFILE; each subsequent step comes from the instance's
    build_steps (profession Dockerfiles, layer-bearing specialties, and the
    harness's CLI layer last), with PARENT_IMAGE pointing at the prior step's
    tag so each Dockerfile's `FROM ${PARENT_IMAGE}` resolves to a
    freshly-built parent. Each step forwards only the build-args its own
    `tag.docker` names (values from the staged container env), so one layer's
    args never surface in another's build; the base forwards the one buster
    its Dockerfile keys on (the --refresh-installs one) — never the weekly
    one, which is the harness layer's. `pull` (--refresh-installs)
    adds `--pull` to the base build so the FROM image itself is refreshed —
    off by default because an ordinary cached launch must work offline.
    `--network=host` because BuildKit's own bridge has had DNS failures
    resolving curl/apt repo hosts on some setups. The build context is always
    the repo root (empty by .dockerignore: no Dockerfile COPYs from it)."""
    tag = image_tag([])
    print(BUILDING_STEP.format(step="base", target=tag))
    docker_subprocess([
        "build", "--network=host", *(["--pull"] if pull else []), "-f", str(BASE_DOCKERFILE), "-t", tag,
        *build_arg_flags((str(ContainerEnvKey.FORCE_INSTALLS_REFRESH),)),
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


def prompt_install_failures(image: str, retry: str) -> None:
    """Read INSTALL_FAILURES_LOG_IN_CONTAINER from the final image; if it's
    non-empty, surface the failed tool names as a press-any-key prompt (so
    Claude Code's TUI takeover doesn't immediately clobber the warning), with
    `retry` — the exact command that retries them (docker_prompts.RETRY_SOLO
    or RETRY_CLUSTER, formatted by the caller, which knows its own entry).
    No-op when the file is missing (no [code] step in the stack) or empty
    (all installs succeeded). Self-contained: the prompt copy lives in
    template_code/docker_prompts and the rendering goes through prompt_keypress;
    nothing flows back to the caller. Uses `docker run --rm --entrypoint cat`
    for the one-shot read — one extra subprocess per launch (~few hundred ms
    after a warm image cache). Called between ensure_image and the run by
    run.py and by cluster/launching.launch, so the list reflects the build
    that just finished.

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
        body=[line.format(retry=retry) for line in INSTALL_FAILURES_BODY],
    )


def effort_args(effort: str | None, claude_args: list[str]) -> list[str]:
    """CLI args pinning the session's effort to the instance's effort word
    (`Instance.effort` — its AI's word for its engine's step, e.g.
    ["--effort", "max"]), or [] when there is no effort or the user passed
    their own --effort through (theirs wins — both the `--effort max` and
    `--effort=max` forms count).

    Why a CLI flag when the same value already ships as a -e env var: on
    newly-launched models (Opus 4.7/4.8, Fable 5) Claude Code pins a fresh
    interactive session's effort to the model's launch default ("high") and
    treats CLAUDE_CODE_EFFORT_LEVEL as a session-only override it neither
    persists nor reflects in the /model UI — its pin logic explicitly checks
    argv for --effort as the user's confirmation. Passing the documented
    flag is the supported way to declare the level so the session both runs
    at it and reports it."""
    harness = active_adapter()
    if not effort or any(a == harness.effort_flag or a.startswith(f"{harness.effort_flag}=") for a in claude_args):
        return []
    return [harness.effort_flag, effort]


def run_cluster_container(session: str, image: str,
                          mounts: Iterable[tuple[str, str]],
                          entrypoint: str, env_files: Iterable[str] = (),
                          contributions: Iterable[DockerContribution] = ()) -> None:
    """The cluster variant of `run_container` — deliberately a sibling here
    rather than a reimplementation in the cluster package (the plan's recorded
    decision: composing a `docker run` stays with the code that owns it).

    Much simpler than the instance path ON PURPOSE, and the absences are the
    contract, not oversights:
      - `contributions` — the UNION's tag.docker records — supply `--cap-add`
        and the env forwards exactly as run_container's do (a cluster-wide
        tag is one setting for the one container), but never an entrypoint
        chain: the generated cluster script is the entrypoint, and wrapping
        it is not built (launching.refusal guards). No shipped tag produces a
        cluster-level capability or env forward today — `{firewall}`, the
        only source of either, forbids `cluster` — so this path is exercised
        by a synthetic contribution in the tests until the first one lands;
      - no engine conf at container level — each member's model/effort rides
        its own tmux window's env (the per-pane `-e` property that chose tmux);
      - no staged global mounts — the cluster assembles its own mount PAIRS
        (pairs, not a dict: a shared credentials file is the SOURCE of one
        mount per member, so sources repeat legally); `env_files` are the
        `--env-file`s for the members' AIs' key files.
    The entrypoint is the generated cluster script (tmux session, one window
    per member, the shell window last); it is the container's PID 1 and holds
    the container open across detaches exactly like the solo muxer script."""
    if leaked := [str(k) for k in ContainerEnvKey.instance_scoped_keys() if str(k) in staged_env()]:
        # A member's identity rides its own pane's env (launching.prepare);
        # container-wide it would be one member's name in every tab. Only
        # set_container_env belongs in a cluster's staging, never
        # set_instance_env — a programming error, refused before docker runs.
        raise RuntimeError(f"instance-scoped env staged for a cluster container: {', '.join(leaked)}")
    contributions = list(contributions)
    container_name = f"{CONTAINER_NAME_PREFIX}{cluster_container_id(session)}"
    set_terminal_title(f"cluster {session}")
    _interactive_docker_run(
        ["run", "--rm", "-it", "--name", container_name, "-e", "TERM",
         "--entrypoint", entrypoint]
        + [f"--cap-add={cap}" for c in contributions for cap in c.cap_add]
        + env_forward_flags(contributions)
        + [arg for source, target in mounts
           for arg in ("-v", f"{source}:{target}")]
        + container_env_args()   # the CONTAINER half's always-on -e set (BASH_ENV, service tokens) — set_container_env over the union probe; never a member's identity
        + [arg for path in env_files for arg in ("--env-file", path)]   # the members' AIs' key files (container-wide: one container)
        + [image])


def run_container(inst: Instance, image: str, claude_args: list[str], resume_flag: list[str],
                  *, interactive: bool = True, print_prompt: str | None = None,
                  stream_renderer: Callable[[Iterable[str]], None] | None = None) -> None:
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

    `interactive` (default True) allocates a TTY (`-it`) for the normal
    interactive Claude Code session. The quickie tool passes
    `interactive=False` + `print_prompt="<question>"` to run one-shot print
    mode (`claude -p "<question>"`): no TTY (so it works piped / from a
    script), the container exits after the answer. `-p` leads the claude argv
    so it reads as `claude -p "<q>" [flags]`.

    `stream_renderer`, when given (quickie's stream-json path), pipes docker's
    stdout line-by-line through it instead of inheriting the terminal, so the
    renderer can show progress and stream the answer; without it, docker keeps
    the terminal as before. The caller supplies the matching claude flags
    (`--output-format stream-json …`) via `claude_args`.

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
    # {muxer}: the agent runs inside a multiplexer, so the command becomes a
    # generated startup script instead of claude's own argv. Assembled here
    # because this is where that argv is known, and only for an interactive
    # launch — quickie's print mode has no terminal to split.
    harness = active_adapter()
    agent_argv = (
        [harness.binary]
        + effort_args(inst.effort, claude_args)
        + resume_flag
        + list(inst.claude_args)
        + claude_args
    )
    muxed = interactive and inst.is_muxer
    if muxed:
        # {muxer} is the chain's TERMINAL link: the agent's argv is baked into the
        # generated script (quoting it through two more shells is where a flag
        # value with spaces would come apart), so nothing follows it.
        from .cluster import solo
        solo.install_launcher(inst, tuple(agent_argv))

    entry_flags, inner_links = entrypoint_chain(contributions)
    # What the container is actually told to run, after the image name:
    #   no wrapper      -> the agent's flags (the harness layer's ENTRYPOINT is its binary)
    #   wrapper(s)      -> the remaining links, then the agent command explicitly
    #   ...ending in {muxer} -> nothing more; its script carries the agent itself
    if muxed:
        command = inner_links
    elif entry_flags:
        command = inner_links + agent_argv
    else:
        command = agent_argv[1:]        # the binary comes from the image ENTRYPOINT (the harness layer's)

    # Spawn the updater BEFORE docker_subprocess (which blocks for the
    # container's lifetime). No-op for non-{firewall} launches.
    start_firewall_updater(container_name)
    args = (
        ["run", "--rm", *(["-it"] if interactive else []), "--name", container_name, "-e", "TERM"]
        + [f"--cap-add={cap}" for c in contributions for cap in c.cap_add]
        + entry_flags
        + [arg for src, tgt in _docker_mounts for arg in ("-v", f"{src}:{tgt}")]
        + container_env_args()             # always-on -e flags (status line, BASH_ENV, cred tokens)
        + key_env_file_args([inst.ai.name] if inst.ai else [])   # the AI's API key file, when the operator keeps one
        + conf_env_args(inst.conf)         # -e flags setting each engine-conf key=value in the container
        + env_forward_flags(contributions)  # tag-conditional -e flags (WHITELIST_ADDRESSES)
        + [image]
        + ([harness.print_flag, print_prompt] if print_prompt is not None else [])   # one-shot print mode (quickie)
        + command                          # see the chain assembly above
    )
    if stream_renderer is not None:
        docker_stream_subprocess(args, stream_renderer)
    elif interactive:
        _interactive_docker_run(args)   # owns the terminal → repair it after
    else:
        docker_subprocess(args)
