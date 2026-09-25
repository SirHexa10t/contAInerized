"""Launching a cluster — the integration step: state on disk → one running
container with N members, each a full Claude session in its own tmux window.

This module ASSEMBLES; docker execution stays in `docker_config`
(`ensure_image` for the union stack, `run_cluster_container` for the run), per
the plan's decision that the one shared concern — composing a `docker run` —
lives with the code that already owns it.

The spike's three recipes (plans/cluster_plan.md, "Research spike — CLOSED")
are baked in here:

- **the kill-switch is unset** in the generated entrypoint
  (`CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC` is sticky — `=0` still
  disables), which is what activates sibling messaging;
- **every member gets its own config dir** (`CLAUDE_CONFIG_DIR=
  /cluster/members/<id>` — riding the one /cluster mount, so no extra mounts)
  with its own persona CLAUDE.md, merged settings, and commands installed by
  the ordinary `agents_crud` pipeline, plus its **`sessions/` symlinked to the
  shared `/cluster/sessions/`** so siblings discover each other despite the
  isolation;
- **members are addressable by id**: `CLAUDE_CODE_SESSION_NAME=<member-id>`
  rides each window's env, so `ListAgents` shows `researcher__primary`, not
  `workspace-e4`.

**One launch core, two shapes (2026-09-15).** Each member is staged by the
same `staging.stage_instance` a solo instance runs (state dir installs, login
files, key preflight, the read-only settings and commands shadows), and the
container is staged by the same core calls over the UNION probe —
`apply_tags` (caches), `set_container_env` (busters, BASH_ENV, the union's
toolkit and creds INSTALL flags, tokens), `plant_user_extras`,
`optional_creds_mounts` — with every mount going through
`docker_config.add_docker_mount`, the one accumulator and collision rule.
What stays cluster-shaped: a member's identity (status line, config dir,
session name) rides its own pane's env, never the container's.

**Container-level tags are the CLUSTER's, never a member's own (2026-09-16).**
One container means one set of capabilities, mounts, env forwards and one
workspace mount, so `{dood}` and `{ro}` are picked cluster-wide (every member
inherits them) and the union probe carries them into the one container —
`apply_tags` stages the socket mount and the GID, `run_cluster_container`
adds the capabilities and env forwards, the workspace mount goes `:ro`. A
member adding one itself is refused with the fix named, from data: the tag's
`forbid_on = ["member"]` in tag.info (the scan insists every container-level
tag declares it), judged by `Cluster.member_instance` per scope. What is still
mechanism: wrapping the cluster's generated entrypoint with another tag's
chain is not built, so `{firewall}` forbids `cluster` too and `refusal` keeps
a safety net for a wrapper entrypoint other than `{muxer}`'s solo script.
"""

from __future__ import annotations

import dataclasses
from pathlib import Path

from ..ai import DEFAULT_HARNESS_KEY, adapter_for, refusal_for
from ..agents_crud import compute_resume_flag
from ..claude_code_config import build_cluster_status_line, optional_creds_line
from ..container_env import ContainerEnvKey, set_container_env
from ..cluster_work_protocol import (
    CONFIG_IN_CONTAINER as PROTOCOL_CONF_TARGET,
    PACKAGE_IN_CONTAINER as PROTOCOL_PACKAGE_TARGET,
    PROTOCOL_DIR_IN_CONTAINER,
)
from ..cluster_work_protocol.queue import CURSORS_DIRNAME
from ..docker_config import add_docker_mount, effort_args, ensure_image, prompt_install_failures, run_cluster_container, staged_mounts
from ..file_access import is_file, write_text
from ..paths import (
    CLUSTER_IN_CONTAINER, CLUSTER_PROTOCOL_CONF,
    CLUSTER_WORK_PROTOCOL_DIR, CLAUDE_CONFIG_IN_CONTAINER,
    RO_MOUNT_OPTION, TMUX_CONF_IN_CONTAINER, WORKSPACE_IN_CONTAINER,
    auth_file_mounts, base_mounts, cluster_banner_path, cluster_member_dir, cluster_path,
    container_transcripts_dir, key_file,
)
from ..staging import stage_instance
from ..tag_handlers import apply_tags
from ..template_code.docker_prompts import RETRY_CLUSTER
from ..tags import DockerContribution, Instance, Registry, TagError, resolve_build
from ..user_additions import optional_creds_mounts, plant_user_extras
from . import backend, herdr, launch_plan, tmux
from .member import ClusterError, Member
from .state import Cluster, picker_order

# The generated entrypoint, beside the banner in the cluster dir — so it rides
# the /cluster mount and the exact script that ran is readable afterwards.
SCRIPT_NAME = "cluster-start.sh"
# Which multiplexer assembles it: `backend()` — the package-level switch
# (the operator's ui_profile.toml preference, herdr by default), shared with
# the solo path so one setting steers every {muxer} shape alike.
# Sticky kill-switch (see the module docstring): unset at the entrypoint, per
# the spike. This re-admits Statsig flag/usage traffic FOR CLUSTER CONTAINERS —
# accepted knowingly (plan: "Telemetry re-admitted"); solo instances keep the
# image's kill-switch untouched.
MESSAGING_KILL_SWITCH = "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC"
# Config-dir children that must exist per member but live centrally: sessions/
# is HOW siblings discover each other (registration is per-config-dir — spike),
# skills and keybindings ship at ~/.claude via the base mounts and would
# silently vanish from a member that looks only in its own config dir.
SHARED_SESSIONS = CLUSTER_IN_CONTAINER / "sessions"
_SHARED_LINKS = ("skills", "keybindings.json")


def container_member_dir(session: str, member_id: str) -> Path:
    """A member's config dir as the CONTAINER sees it — derived from the host
    builder through the /cluster mount, so the two spellings cannot drift."""
    relative = cluster_member_dir(session, member_id).relative_to(cluster_path(session))
    return CLUSTER_IN_CONTAINER / relative


def member_instances(cluster: Cluster,
                     registry: Registry) -> list[tuple[Member, Instance]]:
    """Each member as an ordinary `Instance` (`Cluster.member_instance` — the
    whole point of "a member is an instance in all but placement": persona
    installs, settings merging, engine conf, and claude_args all come from
    the one existing pipeline), turning either of its failure encodings into
    a LOUD STOP naming the member: launching a cluster with a silently-
    degraded member would be the cowork lesson (half-configured peers)
    relearned. The picker takes the same two encodings the other way — a
    red row, not a crash — which is why the method itself never raises."""
    pairs: list[tuple[Member, Instance]] = []
    for member in cluster.members:
        inst = cluster.member_instance(member, registry)
        if inst is None:
            raise ClusterError(
                f"member {member.id!r}: no agent {member.agent!r} in agents/")
        if not inst.is_startable:
            # Per problem, its own words: a tag that is not one at all, and a
            # real one the member cannot carry as its own (`forbidden`, with
            # the scope note saying where it goes — cluster-wide, for {dood}).
            details = "; ".join(
                f"{p.label} ({p.hint})" if p.reason == "forbidden" else f"{p.label} (unknown tag)"
                for p in inst.invalid_tags)
            raise ClusterError(
                f"member {member.id!r} carries tag(s) it cannot: {details} — "
                f"edit its tags from the picker (F2)")
        pairs.append((member, inst))
    return pairs


def refusal(pairs: list[tuple[Member, Instance]]) -> str | None:
    """Why this cluster cannot launch, or None — the two checks that are
    MECHANISM, not data. Everything about WHERE a tag may go is data now
    (`forbid_on` in tag.info, judged by `Cluster.member_instance` per scope
    and surfaced by `member_instances` before this runs): a cluster-wide
    `{dood}` is fine and its socket, GID and mounts reach the one container
    through `apply_tags` over the union probe; a member's own `{dood}` is a
    `forbidden` TagProblem naming the fix.

    What stays here: a member in a harness without an adapter cannot run
    (same rule and message as a solo instance, named per member); and a
    wrapper ENTRYPOINT at any level other than `{muxer}`'s solo startup
    script, because wrapping the cluster's generated entrypoint with another
    tag's chain is not built (plans/ISSUES.md) — `{firewall}` declares
    forbid_on cluster for that reason, so today this is the safety net for a
    wrapper tag that does not."""
    from . import solo
    for member, inst in pairs:
        if inst.harness is not None and (refused := refusal_for(inst.harness.name, inst.harness.label)) is not None:
            return f"member {member.id!r}:\n{refused}"
    for member, inst in pairs:
        for contribution in inst.docker_contributions:
            # muxer's tag.docker declares the CONTAINER path of the solo
            # startup script — compare against that exact spelling
            # (solo.CONTAINER_SCRIPT), which a test pins to the tag file.
            if contribution.entrypoint is not None and contribution.entrypoint != solo.CONTAINER_SCRIPT:
                return (f"member {member.id!r} carries a tag with a wrapper entrypoint "
                        f"({contribution.entrypoint}) — wrapping a cluster's entrypoint script is not "
                        f"built yet; launch that agent as a solo instance")
    return None


@dataclasses.dataclass(frozen=True)
class PreparedLaunch:
    """Everything a `docker run` needs, assembled and written to disk — held as
    a value so the CLI can show it (--dry-run) and tests can assert on it
    without docker existing.

    `mounts` is (host source, container target[:ro]) PAIRS — a snapshot of
    docker_config's accumulator, which since 2026-09-15 holds pairs for the
    same reason this record always did: the shared credentials file is the
    SOURCE of one mount per member (docker happily repeats a source), and the
    source-keyed dict it used to be silently left only the LAST member with
    credentials, caught while writing the test that now pins this shape."""
    cluster: Cluster
    image_probe: Instance                 # the union build ensure_image consumes
    plan: launch_plan.LaunchPlan
    script_host: Path
    script_container: str
    mounts: tuple[tuple[str, str], ...]
    env_files: tuple[str, ...] = ()       # `--env-file`s: the members' AIs' key files that exist (credentials/keys/<ai>.env)
    notices: tuple[str, ...] = ()         # what stage_credentials had to say, printed by launch() before docker runs
    cred_names: tuple[str, ...] = ()      # the operator's optional-creds services mounted (user_extras/optional_creds/), named in the banner like a solo launch's
    contributions: tuple[DockerContribution, ...] = ()   # the union's tag.docker records — cluster-wide capabilities and env forwards for run_cluster_container (never entrypoints)


def _union_probe(cluster: Cluster, pairs: list[tuple[Member, Instance]],
                 registry: Registry) -> Instance:
    """A synthetic Instance carrying the UNION of the members' image-bearing
    tags — one container means one image, so its layers are everyone's layers
    (the plan's recorded decision). Engines don't shape images and policies
    don't either, so only professions and specialties union; `resolve_build`'s
    ordering machinery then produces the same canonical chain `ensure_image`
    builds for solo instances."""
    from ..tags.lego import AgentBuild
    professions: list[str] = []
    specialties: list[str] = []
    for member, _ in pairs:
        build = cluster.member_build(member)   # cluster tags included
        for name in build.professions:
            if name not in professions:
                professions.append(name)
        for name in build.specialties:
            if name not in specialties:
                specialties.append(name)
    first = pairs[0][0]
    union = AgentBuild(professions=tuple(professions),
                       specialties=tuple(specialties))
    return Instance(
        agent=first.agent, md_path=pairs[0][1].md_path, session=cluster.session,
        workspace=str(cluster.project), is_brand_new=False,
        state_dir_override=cluster_path(cluster.session),
        **resolve_build(union, first.agent, registry))


def _setup_commands(cluster: Cluster) -> tuple[str, ...]:
    """The entrypoint's pre-tmux filesystem plumbing, one line per fact:

    - the shared sessions dir, and every member's `sessions/` symlinked to it
      (discovery is per-config-dir; the shared dir is what makes isolated
      members visible to each other — the spike's OPEN #3 answer);
    - the work-protocol's home (`/cluster/protocol` + its cursors/) — created
      HERE, member-owned, never as a docker mountpoint parent (those arrive
      root-owned: the recorded herdr lesson);
    - skills and keybindings symlinked from the shared ~/.claude mounts, which
      a member's CLAUDE_CONFIG_DIR would otherwise hide.

    `ln -sfn` so a relaunch over existing links is idempotent."""
    lines = [f"mkdir -p {SHARED_SESSIONS}",
             f"mkdir -p {PROTOCOL_DIR_IN_CONTAINER / CURSORS_DIRNAME}"]
    for member in cluster.members:
        config = container_member_dir(cluster.session, member.id)
        lines.append(f"mkdir -p {config}")
        lines.append(f"ln -sfn {SHARED_SESSIONS} {config}/sessions")
        for name in _SHARED_LINKS:
            lines.append(
                f"ln -sfn {CLAUDE_CONFIG_IN_CONTAINER}/{name} {config}/{name}")
    return tuple(lines)


def prepare(cluster: Cluster, registry: Registry, *, refresh_installs: bool = False) -> PreparedLaunch:
    """Assemble the launch: refuse what can't be honoured, install every
    member's state, resolve per-member env and argv, write the banner and the
    entrypoint script, and collect the mount set. Everything on disk after
    this; nothing docker yet."""
    # ONE reorder at the boundary: everything downstream — window creation,
    # `^b 1..9` numbers, the landing window, install iteration — follows the
    # derived picker order, so no consumer can disagree with another.
    cluster = dataclasses.replace(
        cluster, members=picker_order(cluster.members, registry))
    pairs = member_instances(cluster, registry)
    if (reason := refusal(pairs)) is not None:
        raise ClusterError(reason)

    probe = _union_probe(cluster, pairs, registry)
    # CONTAINER-LEVEL staging, the same core calls a solo launch makes, over
    # the union probe — one container has one image, one env, one set of
    # mounts, and N members share them: the tags' contributions and handlers
    # ([code]'s caches, pruned; container-level tags never reach here —
    # refusal() kept them out), the container's env (busters, BASH_ENV, the
    # UNION's toolkit and creds INSTALL flags, service tokens — never a
    # member's identity, which rides its pane), the operator's first-launch
    # files. Until 2026-09-15 the cluster skipped all three: its image was
    # built from the Dockerfile's ARG defaults and its shells had no BASH_ENV.
    try:
        apply_tags(probe)
    except (ValueError, RuntimeError) as e:
        raise ClusterError(str(e)) from None
    set_container_env(probe.professions, refresh_installs=refresh_installs)   # `cluster.py launch --refresh-installs`, or run.py's flag through its cluster branch
    plant_user_extras(probe)

    env_for: dict[str, dict[str, str]] = {}
    command_for: dict[str, tuple[str, ...]] = {}
    notices: list[str] = []
    for member, inst in pairs:
        config = container_member_dir(cluster.session, member.id)
        # The adapter is the MEMBER's harness's (refusal() has made sure it
        # has one). The member's own staging is the ONE function every run
        # shape calls (launch/staging.py) — state dir installs, login files,
        # key preflight, and the mounts for them INTO the member's config dir
        # (the relocation variable points there, so even the file that sits
        # at HOME by default lives inside it; the settings and commands are
        # read-only over their rw view through the /cluster mount — the solo
        # shadowing, from the one definition). A stop names the member.
        harness = adapter_for(inst.harness.name if inst.harness else DEFAULT_HARNESS_KEY)
        try:
            staged = stage_instance(inst, registry, harness=harness, config=str(config), relocated=True)
        except (TagError, RuntimeError) as e:
            raise ClusterError(f"member {member.id!r}: {e}") from None
        notices.extend(n for n in staged.notices if n not in notices)
        # The engine conf rides the WINDOW env — the per-pane `-e` property
        # that chose tmux — so two members genuinely run different models.
        env_for[member.id] = {
            **inst.conf,
            harness.config_dir_env: str(config),
            **({harness.session_name_env: member.id} if harness.session_name_env else {}),
            # The bottom status line — member id, project, user, cluster,
            # tags. Per-member and so per-TAB env: container-wide it could
            # only carry one member's line, which is why members had a blank
            # bottom row while solo instances have always had this one
            # (operator report, 2026-09-02).
            ContainerEnvKey.AGENT_STATUS_LINE.value:
                build_cluster_status_line(inst, member.id),
            # Where THIS member's transcripts are — its own config dir's
            # `projects/`, never /home/claude/.claude's. The in-container
            # readers (dump_last_msg, find_in_history) follow this rather
            # than a hardcoded path, which is what made them read another
            # member's dir, or nothing at all, before 2026-09-19.
            ContainerEnvKey.AGENT_TRANSCRIPTS_DIR.value:
                str(container_transcripts_dir(config)),
        }
        command_for[member.id] = (
            harness.binary,
            *effort_args(inst.effort, []),
            *compute_resume_flag(inst),
            *inst.claude_args,
        )

    plan = launch_plan.build(cluster, env_for=env_for, command_for=command_for,
                             personal_workspaces=False)
    # Every mount goes through docker_config.add_docker_mount — the ONE
    # accumulator and the ONE collision rule a solo launch has (a shadowing
    # target is refused here, cleanly, not by docker at run time). Sources
    # repeat legally: the shared login file mounts once per member.
    for host, target in plan.mounts().items():
        # A cluster-wide `{ro}` (workspace_readonly — forbid_on member, so
        # only the shared set can carry it) makes the ONE workspace mount
        # read-only, exactly as set_container_mounts does for a solo
        # instance; until 2026-09-16 the cluster ignored the key.
        if probe.workspace_readonly and target == str(WORKSPACE_IN_CONTAINER):
            target = f"{target}:{RO_MOUNT_OPTION}"
        add_docker_mount(host, target)
    # The work-protocol rides every cluster launch: the package (RO, whole —
    # the `_cluster` layer's cluster-chat shim module-runs it off /opt) and
    # its tunables file. Both /opt-rooted so no mountpoint parent lands
    # inside member-writable trees.
    add_docker_mount(CLUSTER_WORK_PROTOCOL_DIR, f"{PROTOCOL_PACKAGE_TARGET}:{RO_MOUNT_OPTION}")
    add_docker_mount(CLUSTER_PROTOCOL_CONF, f"{PROTOCOL_CONF_TARGET}:{RO_MOUNT_OPTION}")
    # The always-on base set, exactly as every solo launch mounts it. Nothing
    # here is optional for a cluster: the entrypoint SOURCES tmux.conf (its
    # `-q` means a missing mount silently boots a session with no quit/help/
    # mouse keys), the help popup cats muxer-help.txt, BASH_ENV points at the
    # bashrc, member settings reference the statusline script, and the
    # per-member skills/keybindings symlinks point INTO these mounts. Found
    # missing by an operator question, not a boot — recorded so it stays pinned.
    for source, target in base_mounts():
        add_docker_mount(source, target)

    script_host = cluster_path(cluster.session) / SCRIPT_NAME
    if backend() == "herdr":
        # No banner file: herdr renders the member line from the per-pane
        # metadata the script reports (`workspace report-metadata`), so the
        # file tmux's status bar cats has no reader here. Writing it anyway
        # left every default-backend launch producing a file nobody opened.
        text = herdr.script(
            cluster.session, plan.panes(),
            shell_cwd=plan.container_shell_cwd,
            unset_env=(MESSAGING_KILL_SWITCH,),
            setup_commands=_setup_commands(cluster))
    else:
        # tmux's status bar cats this file (`plan.container_banner` is its
        # in-container path), so the backend that reads it writes it.
        write_text(cluster_banner_path(cluster.session),
                   tmux.banner_text(cluster.ids, project=str(cluster.project)))
        text = tmux.script(
            cluster.session, plan.panes(),
            banner=plan.container_banner,
            shell_cwd=plan.container_shell_cwd,
            unset_env=(MESSAGING_KILL_SWITCH,),
            setup_commands=_setup_commands(cluster),
            user_conf=TMUX_CONF_IN_CONTAINER)
    write_text(script_host, text)
    script_host.chmod(0o755)
    # The free shell pane keeps the default harness's login at the DEFAULT
    # location too (HOME / the default config root), for a human running the
    # CLI by hand there — not for any member, whose files sit in its own dir.
    for source, target in auth_file_mounts(adapter_for(DEFAULT_HARNESS_KEY),
                                           config=str(CLAUDE_CONFIG_IN_CONTAINER), relocated=False):
        add_docker_mount(source, target)
    # The operator's optional credentials (user_extras/optional_creds/), as
    # every solo instance mounts them: presence on the host is the opt-in,
    # and one shared container cannot hold them per member — N members share
    # them exactly as N solo instances would (plans/ISSUES.md records the
    # concurrent-refresh consequence). Last, so a `home/` entry that would
    # shadow a launcher mount is refused with the friendlier message.
    try:
        cred_names = optional_creds_mounts()
    except RuntimeError as e:
        raise ClusterError(str(e)) from None
    ai_names = sorted({inst.ai.name for _, inst in pairs if inst.ai})
    env_files = tuple(str(key_file(name)) for name in ai_names if is_file(key_file(name)))
    return PreparedLaunch(
        cluster=cluster, image_probe=probe,
        plan=plan, script_host=script_host,
        script_container=str(CLUSTER_IN_CONTAINER / SCRIPT_NAME),
        mounts=staged_mounts(), env_files=env_files, notices=tuple(notices),
        cred_names=tuple(cred_names), contributions=tuple(probe.docker_contributions))


def launch(cluster: Cluster, registry: Registry, *, refresh_installs: bool = False) -> None:
    """The whole thing: prepare, build the union image, surface its failed
    optional installs with the cluster's own retry command, hand the terminal
    to the container. Blocks until the cluster session ends (detaching keeps
    it running — the muxer contract). `refresh_installs` is the CLI flag,
    busting every tool layer of the union build as run.py's does for a solo.

    Announces the messaging trade before the terminal changes hands: enabling
    sibling messaging re-admits Statsig traffic for THIS container (the plan's
    recorded, accepted cost) — stated per launch so it is never a surprise."""
    prepared = prepare(cluster, registry, refresh_installs=refresh_installs)
    print(f"  Cluster '{cluster.session}' — {len(cluster.members)} member(s), "
          f"project {cluster.project}")
    for member in prepared.cluster.members:   # picker order — window order
        print(f"    {member.id}")
    print("  Sibling messaging is ON (kill-switch unset for this container —\n"
          "  accepts Anthropic flag/usage traffic; solo instances are unaffected).")
    if backend() == "herdr":
        print("  Backend: HERDR — members listed by name in the sidebar\n"
              "  (prefix+b), live idle/working state; the prefix is ctrl+b:\n"
              "  press it, release, then the key. Cycle members: prefix+n/p\n"
              "  or prefix+1..9. prefix+q DETACHES (everything keeps\n"
              "  running); alt+/ is the help. alt+q (Y/n popup, Enter\n"
              "  confirms) or `herdr server stop` ends the cluster.")
    else:
        print("  Cycle members: ^b n / ^b p, ^b <number>, or click a name in the\n"
              "  status bar. ^b d detaches; everything keeps running.")
    if (creds_line := optional_creds_line(prepared.cred_names)) is not None:
        print(creds_line)
    for notice in prepared.notices:
        print(notice)
    image = ensure_image(prepared.image_probe, pull=refresh_installs)
    # The same failed-installs gate a solo launch has, before the terminal
    # changes hands — with this shape's retry spelling.
    prompt_install_failures(image, RETRY_CLUSTER.format(session=cluster.session))
    run_cluster_container(cluster.session, image, prepared.mounts,
                          prepared.script_container, env_files=prepared.env_files,
                          contributions=prepared.contributions)
