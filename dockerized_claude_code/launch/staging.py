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
from .paths import auth_file_mounts, commands_mount, settings_mount
from .tags import Instance, Registry


@dataclasses.dataclass(frozen=True)
class StagedInstance:
    """What staging one agent produced, as a value: the `(host source,
    container target[:ro])` pairs its files were staged to mount as (already
    in docker_config's accumulator — reported so a caller or a test can see
    them), and what the credentials staging had to say, for the caller to
    print (once per launch, however many members said the same thing)."""
    mounts: tuple[tuple[str, str], ...]
    notices: tuple[str, ...]


def stage_instance(inst: Instance, registry: Registry, *, harness: Adapter,
                   config: str, relocated: bool) -> StagedInstance:
    """Install `inst`'s state and stage what mounts it — into
    `docker_config.add_docker_mount`, the one accumulator both launch shapes
    run from. `harness` is the adapter of the CLI this agent runs in (the
    adopted one for a solo launch, the member's own in a cluster), `config`
    the CLI's config root inside the container, `relocated` whether the
    relocation variable points at it.

    Raises `TagError` for a policy conflict (install_settings) or a command
    name collision (install_commands), and `RuntimeError` for a key file
    docker would mis-read (stage_credentials) or a mount that would shadow
    one already staged (add_docker_mount) — each caller turns those into its
    own clean stop; nothing here prints or exits."""
    ensure_dir(inst.state_dir)
    install_latest_md(inst)
    install_settings(inst, registry)
    install_commands(inst)
    notices = stage_credentials(harness, [inst.ai] if inst.ai else [])
    mounts = [
        # The generated settings and the assembled commands, READ-ONLY over
        # the rw view of the same paths — the agent reads its policies and
        # commands but cannot relax or rewrite them.
        settings_mount(inst.state_dir, config),
        commands_mount(inst.state_dir, config),
        # The harness's login files, where THIS shape's CLI expects them.
        *auth_file_mounts(harness, config=config, relocated=relocated),
    ]
    for source, target in mounts:
        add_docker_mount(source, target)
    return StagedInstance(mounts=tuple(mounts), notices=tuple(notices))
