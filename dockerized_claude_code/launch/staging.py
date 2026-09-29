"""Staging ONE agent for a container — the step every run shape shares.

A solo instance (`run.setup_state`), a quickie question (`quickie/ask`) and
each member of a cluster (`cluster/launching.prepare`) all need the same
things done for the agent before docker runs: its state dir installed
(persona CLAUDE.md, merged settings, assembled slash commands), its harness's
login files in place and its AI's key file checked, and the mounts that put
the installed files where the CLI reads them. Until 2026-09-15 each shape
spelled that sequence itself, and they drifted: a member's commands dir was
writable where a solo instance's is read-only, the quickie mounted a commands
dir it never installed (docker root-created the source), and a member's
policy-conflict error escaped without the member's name. One function, three
callers — the shapes differ only in WHERE the CLI's config root is inside the
container (`config`) and whether the relocation variable points there
(`relocated`), which is exactly what `paths.auth_file_mounts` already models.

What stays with each shape, deliberately: the container-level staging (the
workspace mount, the base set, tag contributions, caches, the env) — one
container has one of each, N members share it, and each shape calls the same
core functions for it (`apply_tags`, `set_container_env`, `optional_creds_mounts`)
over its own instance or the cluster's union probe — and how an error reaches
the operator (`call_or_exit` for a solo launch, a `ClusterError` naming the
member for a cluster).
"""

from __future__ import annotations

import dataclasses

from .agents_crud import install_commands, install_latest_md, install_settings
from .ai import Adapter
from .docker_config import add_docker_mount, stage_credentials
from .file_access import ensure_dir
from .paths import auth_file_mounts, commands_mount, policy_file_mount, settings_mount
from .tags import Instance, Registry


@dataclasses.dataclass(frozen=True)
class StagedInstance:
    """What staging one agent produced, as a value: the `(host source,
    container target[:ro])` pairs its files were staged to mount as (already
    in docker_config's accumulator — reported so a caller or a test can see
    them), and what the staging had to say — a policy word its harness
    cannot grant, the credentials check's findings, a picked model that
    cannot run — for the caller to print (once per launch, however many
    members said the same thing)."""
    mounts: tuple[tuple[str, str], ...]
    notices: tuple[str, ...]


def stage_instance(inst: Instance, registry: Registry, *, harness: Adapter,
                   config: str, relocated: bool, who: str) -> StagedInstance:
    """Install `inst`'s state and stage what mounts it — into
    `docker_config.add_docker_mount`, the one accumulator both launch shapes
    run from. `harness` is the adapter of the CLI this agent runs in (the
    adopted one for a solo launch, the member's own in a cluster), `config`
    the CLI's config root inside the container, `relocated` whether the
    relocation variable points at it, and `who` the agent as this launch's
    operator knows it (the instance id solo, the member id in a cluster),
    for the notices about it alone.

    Raises `TagError` for a policy conflict or a policy the harness cannot
    enforce (install_settings) or a command name collision
    (install_commands), and `RuntimeError` for a key file
    docker would mis-read (stage_credentials) or a mount that would shadow
    one already staged (add_docker_mount) — each caller turns those into its
    own clean stop; nothing here prints or exits."""
    ensure_dir(inst.state_dir)
    install_latest_md(inst)
    notices = list(install_settings(inst, registry, harness))
    install_commands(inst)
    notices += stage_credentials(harness, [inst.ai] if inst.ai else [])
    notices += [notice for notice in (stale_model_notice(inst, who), stale_effort_notice(inst, who)) if notice]
    policy_file = registry.harnesses[harness.key].policy_file
    mounts = [
        # The generated settings and the assembled commands, READ-ONLY over
        # the rw view of the same paths — the agent reads its policies and
        # commands but cannot relax or rewrite them.
        settings_mount(inst.state_dir, config),
        commands_mount(inst.state_dir, config),
        # The harness's own rules file, when it keeps one — the same shadow.
        *([policy_file_mount(inst.state_dir, config, policy_file)] if policy_file else []),
        # The harness's login files, where THIS shape's CLI expects them.
        *auth_file_mounts(harness, config=config, relocated=relocated),
    ]
    for source, target in mounts:
        add_docker_mount(source, target)
    return StagedInstance(mounts=tuple(mounts), notices=tuple(notices))


def stale_model_notice(inst: Instance, who: str) -> str | None:
    """The line for a picked model `inst` cannot run (`Instance.stale_model`),
    or None: which pick, why — the vendor's retirement date, or not this AI's
    id at all — and what runs instead, since the fallback can move either way
    (a cheap pick gone stale on a `best` engine runs the dearest model).
    Named for `who`, because in a cluster several members may say it."""
    stale = inst.stale_model
    if stale is None or inst.ai is None:
        return None
    instead = f"{inst.engine.label}'s {inst.engine_model}" if inst.engine and inst.engine_model else "its engine's model"
    level = f" at {inst.effort}" if inst.effort else ""
    dropped = f", with its effort {inst.stale_effort}," if inst.stale_effort else ""
    return (f"  Note: {who}'s model {stale.spelling}{dropped} is {stale.why(inst.ai.label)} — dropped for this "
            f"launch, running {instead}{level}; F2 in the picker picks another.")


def stale_effort_notice(inst: Instance, who: str) -> str | None:
    """The line for a stored effort level the model `inst` runs does not take
    (`Instance.stale_effort`), or None: both levels — the pin and the one
    that runs instead, chosen PRESERVING DIRECTION (`Model.effort_for`: the
    nearest level at or below the pin, else the lowest), so a pin kept low
    for cost never lands on the top — and the model, named for `who`."""
    model = inst.running_model
    if inst.stale_effort is None or model is None or inst.stale_model is not None:
        return None                   # beside a stale model, the model's notice covers the pair
    running = inst.effort
    instead = f"running {running}" if running else "sending no effort level"
    return (f"  Note: {who}'s effort {inst.stale_effort} is not one {model.id} takes — {instead} instead; "
            f"F2 in the picker picks another.")

