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

**What a member may NOT bring (yet): container-level docker contributions.**
One container means one set of capabilities, mounts, and entrypoints, and this
integration makes no attempt to merge them — a member carrying `{firewall}` or
`{dood}` is refused with the tag named, rather than silently launched without
its protection (the worse failure). `{muxer}`'s own entrypoint contribution is
exempt: the cluster script IS that entrypoint's cluster-shaped sibling.
"""

from __future__ import annotations

import dataclasses
from pathlib import Path

from ..agents_crud import (
    compute_resume_flag, install_commands, install_latest_md, install_settings,
)
from ..claude_code_config import build_cluster_status_line
from ..cluster_work_protocol import (
    CONFIG_IN_CONTAINER as PROTOCOL_CONF_TARGET,
    PACKAGE_IN_CONTAINER as PROTOCOL_PACKAGE_TARGET,
    PROTOCOL_DIR_IN_CONTAINER,
)
from ..cluster_work_protocol.queue import CURSORS_DIRNAME
from ..container_env import ContainerEnvKey
from ..docker_config import effort_args, ensure_image, run_cluster_container
from ..file_access import agent_md_index, ensure_dir, write_text
from ..paths import (
    ACCOUNT_FILE, CACHE_MOUNTS, CLUSTER_IN_CONTAINER, CLUSTER_PROTOCOL_CONF,
    CLUSTER_WORK_PROTOCOL_DIR, CREDENTIALS_FILE, CLAUDE_CONFIG_IN_CONTAINER,
    DOCKER_BASE_MOUNTS, RO_MOUNT_OPTION, TMUX_CONF_IN_CONTAINER,
    cluster_banner_path, cluster_member_dir, cluster_path,
    state_settings_path,
)
from ..tags import Instance, Registry, resolve_build
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
    """Each member as an ordinary `Instance` — the whole point of "a member is
    an instance in all but placement": persona installs, settings merging,
    engine conf, and claude_args all come from the one existing pipeline. The
    state dir is the member's own dir inside the cluster.

    A member whose agent or tags no longer resolve is a loud stop naming the
    member: launching a cluster with a silently-degraded member would be the
    cowork lesson (half-configured peers) relearned."""
    index = agent_md_index()
    pairs: list[tuple[Member, Instance]] = []
    for member in cluster.members:
        md_path = index.get(member.agent)
        if md_path is None:
            raise ClusterError(
                f"member {member.id!r}: no agent {member.agent!r} in agents/")
        try:
            # The CLUSTER's tags plus the member's own — `member.build` alone
            # would launch a member without {clstr}, {cc}, or anything else
            # the cluster set for everyone (stored once, cluster-level).
            resolved = resolve_build(cluster.member_build(member),
                                     member.agent, registry)
        except KeyError as error:
            raise ClusterError(
                f"member {member.id!r} references unknown tag {error} — edit "
                f"its tags from the picker (F2)") from error
        pairs.append((member, Instance(
            agent=member.agent, md_path=md_path, session=cluster.session,
            workspace=str(cluster.project), is_brand_new=False,
            state_dir_override=cluster_member_dir(cluster.session, member.id),
            **resolved)))
    return pairs


def refusal(pairs: list[tuple[Member, Instance]]) -> str | None:
    """Why this cluster cannot launch, or None.

    One container, one set of container-level docker features — so a member
    whose tags contribute capabilities, mounts, env forwards, or a foreign
    entrypoint is refused BY NAME rather than launched without them. `{muxer}`'s
    entrypoint (the solo startup script) is the one exemption: this launch
    replaces it with the cluster-shaped script."""
    from . import solo
    offending: list[str] = []
    for member, inst in pairs:
        for contribution in inst.docker_contributions:
            # muxer's tag.docker declares the CONTAINER path of the solo
            # startup script — compare against that exact spelling
            # (solo.CONTAINER_SCRIPT), which a test pins to the tag file.
            foreign_entry = (contribution.entrypoint is not None
                             and contribution.entrypoint != solo.CONTAINER_SCRIPT)
            if (contribution.cap_add or contribution.mounts
                    or contribution.env_forward or foreign_entry):
                offending.append(member.id)
                break
    if not offending:
        return None
    return ("these members carry tags with container-level docker features "
            "(capabilities / mounts / entrypoints), which a shared container "
            f"cannot honour per-member yet: {', '.join(offending)} — remove "
            "those tags (picker F2) or launch them as solo instances")


@dataclasses.dataclass(frozen=True)
class PreparedLaunch:
    """Everything a `docker run` needs, assembled and written to disk — held as
    a value so the CLI can show it (--dry-run) and tests can assert on it
    without docker existing.

    `mounts` is (host source, container target[:ro]) PAIRS, not a dict keyed
    either way: the shared credentials file is the SOURCE of one mount per
    member (docker happily repeats a source), so source keys collide — a
    source-keyed dict silently left only the LAST member with credentials,
    caught while writing the test that now pins this shape."""
    cluster: Cluster
    image_probe: Instance                 # the union build ensure_image consumes
    plan: launch_plan.LaunchPlan
    script_host: Path
    script_container: str
    mounts: tuple[tuple[str, str], ...]


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


def prepare(cluster: Cluster, registry: Registry) -> PreparedLaunch:
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

    env_for: dict[str, dict[str, str]] = {}
    command_for: dict[str, tuple[str, ...]] = {}
    mounts: list[tuple[str, str]] = []
    needs_caches = False
    for member, inst in pairs:
        ensure_dir(inst.state_dir)
        install_latest_md(inst)
        install_settings(inst, registry)
        install_commands(inst)
        config = container_member_dir(cluster.session, member.id)
        # The engine conf rides the WINDOW env — the per-pane `-e` property
        # that chose tmux — so two members genuinely run different models.
        env_for[member.id] = {
            **inst.conf,
            "CLAUDE_CONFIG_DIR": str(config),
            "CLAUDE_CODE_SESSION_NAME": member.id,
            # The bottom status line — member id, project, user, cluster,
            # tags. Per-member and so per-TAB env: container-wide it could
            # only carry one member's line, which is why members had a blank
            # bottom row while solo instances have always had this one
            # (operator report, 2026-09-02).
            ContainerEnvKey.AGENT_STATUS_LINE.value:
                build_cluster_status_line(inst, member.id),
        }
        command_for[member.id] = (
            "claude",
            *effort_args(inst.conf, []),
            *compute_resume_flag(inst),
            *inst.claude_args,
        )
        # Per-member credential/account file mounts INTO the member's config
        # dir (CLAUDE_CONFIG_DIR relocates where claude looks for both). The
        # launcher places credentials host-side — the same trust shape as solo
        # instances, and the reason no agent ever needs to copy a credential.
        member_dir = cluster_member_dir(cluster.session, member.id)
        mounts.append((str(CREDENTIALS_FILE), f"{config}/.credentials.json"))
        mounts.append((str(ACCOUNT_FILE), f"{config}/.claude.json"))
        # The merged settings mount READ-ONLY over their rw view through the
        # /cluster mount — same shadowing trick as solo, same reason: a member
        # must not be able to relax its own policies.
        mounts.append((str(state_settings_path(member_dir)),
                       f"{config}/settings.json:{RO_MOUNT_OPTION}"))
        needs_caches = needs_caches or any(
            p.name == "code" for p in inst.professions)

    plan = launch_plan.build(cluster, env_for=env_for, command_for=command_for,
                             personal_workspaces=False)
    for host, target in plan.mounts().items():
        mounts.append((str(host), target))
    # The work-protocol rides every cluster launch: the package (RO, whole —
    # the `_cluster` layer's cluster-chat shim module-runs it off /opt) and
    # its tunables file. Both /opt-rooted so no mountpoint parent lands
    # inside member-writable trees.
    mounts.append((str(CLUSTER_WORK_PROTOCOL_DIR),
                   f"{PROTOCOL_PACKAGE_TARGET}:{RO_MOUNT_OPTION}"))
    mounts.append((str(CLUSTER_PROTOCOL_CONF),
                   f"{PROTOCOL_CONF_TARGET}:{RO_MOUNT_OPTION}"))
    # The always-on base set, exactly as every solo launch mounts it. Nothing
    # here is optional for a cluster: the entrypoint SOURCES tmux.conf (its
    # `-q` means a missing mount silently boots a session with no quit/help/
    # mouse keys), the help popup cats muxer-help.txt, BASH_ENV points at the
    # bashrc, member settings reference the statusline script, and the
    # per-member skills/keybindings symlinks point INTO these mounts. Found
    # missing by an operator question, not a boot — recorded so it stays pinned.
    for host, target in DOCKER_BASE_MOUNTS.items():
        mounts.append((str(host), str(target)))
    if needs_caches:
        # The toolchain caches [code] members share with every other [code]
        # container — same host dirs, so a warm cache warms the cluster too.
        for cache_host, cache_target in CACHE_MOUNTS.items():
            ensure_dir(cache_host)
            mounts.append((str(cache_host), str(cache_target)))

    write_text(cluster_banner_path(cluster.session),
               tmux.banner_text(cluster.ids, project=str(cluster.project)))
    script_host = cluster_path(cluster.session) / SCRIPT_NAME
    if backend() == "herdr":
        text = herdr.script(
            cluster.session, plan.panes(),
            shell_cwd=plan.container_shell_cwd,
            unset_env=(MESSAGING_KILL_SWITCH,),
            setup_commands=_setup_commands(cluster))
    else:
        text = tmux.script(
            cluster.session, plan.panes(),
            banner=plan.container_banner,
            shell_cwd=plan.container_shell_cwd,
            unset_env=(MESSAGING_KILL_SWITCH,),
            setup_commands=_setup_commands(cluster),
            user_conf=TMUX_CONF_IN_CONTAINER)
    write_text(script_host, text)
    script_host.chmod(0o755)
    return PreparedLaunch(
        cluster=cluster, image_probe=_union_probe(cluster, pairs, registry),
        plan=plan, script_host=script_host,
        script_container=str(CLUSTER_IN_CONTAINER / SCRIPT_NAME),
        mounts=tuple(mounts))


def launch(cluster: Cluster, registry: Registry) -> None:
    """The whole thing: prepare, build the union image, hand the terminal to
    the container. Blocks until the cluster session ends (detaching keeps it
    running — the muxer contract).

    Announces the messaging trade before the terminal changes hands: enabling
    sibling messaging re-admits Statsig traffic for THIS container (the plan's
    recorded, accepted cost) — stated per launch so it is never a surprise."""
    prepared = prepare(cluster, registry)
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
    image = ensure_image(prepared.image_probe)
    run_cluster_container(cluster.session, image, prepared.mounts,
                          prepared.script_container)
