"""What `{muxer}` does to a SOLO launch: run the agent inside a multiplexer.

Without this, the tag only *installs* tmux — the container still hands the
terminal straight to `claude`, so nothing looks different and none of the tag's
promises (an extra shell, surviving a detach, one deliberate way out) are true.
This module is the difference between the tag being installed and the tag being
felt.

**How the command gets replaced.** The base image's `ENTRYPOINT` is `claude`, so
everything after the image name is claude's own argv. To run something else the
entrypoint has to be overridden — the mechanism `{firewall}` already uses. Rather
than ship a static shell script that would have to re-implement the multiplexer
assembly in a second language (and drift from it), the launcher GENERATES the
startup script — from whichever backend `backend()` picks: the operator's
ui_profile.toml preference, herdr by default — into the instance's own state
dir, which is already bind-mounted into the container. One source of truth,
and the exact script that ran is left on disk afterwards for anyone debugging
a launch.

**Composing with other wrappers.** `{muxer}` is the LAST link in
`docker_config.entrypoint_chain`: `{firewall}` (or any future wrapper) runs first,
does its job, and `exec "$@"`s this script, which then starts the multiplexer with
the agent baked in. Nothing follows it, which is why the agent's argv is baked
rather than passed through — see `install_launcher`.
"""

from __future__ import annotations

from pathlib import Path

from ..file_access import write_text
from ..paths import (
    CLAUDE_CONFIG_IN_CONTAINER, HERDR_CONF_IN_CONTAINER, HERDR_SOLO_CONF,
    RO_MOUNT_OPTION, TMUX_CONF_IN_CONTAINER, WORKSPACE_IN_CONTAINER,
)
from ..tags.identity import Instance
from . import backend, herdr, tmux

SCRIPT_NAME = "muxer-start.sh"      # written into the instance state dir each launch
# What the container runs. Declared in `agents/specialty/muxer/tag.docker` too —
# that file is the one the launcher reads, this constant is what writes the file
# it names. test_cluster_solo pins them together.
CONTAINER_SCRIPT = str(CLAUDE_CONFIG_IN_CONTAINER / SCRIPT_NAME)


def herdr_conf_override() -> tuple[Path, str] | None:
    """The (host source, container target[:ro]) for a SOLO herdr launch's
    config — the collapsed-sidebar variant, settings/herdr-solo.toml — or
    None when the backend isn't herdr. Same container target as the shared
    config, so the caller must SWAP the staged mount rather than add one
    (docker_config's target guard rightly refuses a second claimant)."""
    if backend() != "herdr":
        return None
    return HERDR_SOLO_CONF, f"{HERDR_CONF_IN_CONTAINER}:{RO_MOUNT_OPTION}"


def script_paths(inst: Instance) -> tuple[Path, str]:
    """`(host path to write, container path to exec)` for this launch's script.

    Two paths for one file, like the cluster banner: the launcher writes it
    host-side into the state dir, and the container sees that dir mounted at
    CLAUDE_CONFIG_IN_CONTAINER. Deriving the container side from the SAME
    filename keeps them from drifting."""
    return inst.state_dir / SCRIPT_NAME, CONTAINER_SCRIPT


def install_launcher(inst: Instance, agent_argv: tuple[str, ...]) -> str:
    """Write this launch's multiplexer startup script; return the container
    path to run.

    Which backend's script is `backend()`'s call (the ui_profile.toml
    preference, herdr by default) — the same switch a cluster launch reads,
    and the same generated filename either way, so the tag.docker entrypoint
    needs no second spelling.

    `agent_argv` is the full command the container would otherwise have run
    (`claude` plus every flag the launcher assembled). It is baked into the
    script rather than passed through as `"$@"` because the script IS the
    entrypoint: quoting an argv through two shells is exactly where a
    `--append-system-prompt` containing spaces would come apart.

    A name nuance: the instance id is a herdr workspace LABEL but a tmux
    ADDRESS. A character `valid_label` rejects (a dot, from a dotted project
    dir) fails the herdr path loudly at assembly; the tmux path would silently
    mistarget its `-t session:window.pane` calls instead — the loud stop is
    the better half of a pre-existing gap.
    """
    host, container = script_paths(inst)
    agent = tmux.Pane(name=tmux.AGENT_PANE, command=agent_argv,
                      cwd=WORKSPACE_IN_CONTAINER)
    if backend() == "herdr":
        # No unset_env, no setup_commands: enabling sibling messaging (and its
        # telemetry cost) is a CLUSTER trade — a solo instance keeps the
        # image's kill-switch. solo=True is the tmux solo layout translated:
        # the agent IS the workspace root pane, the free shell splits beneath
        # it, one tab named after the agent (the tab row carries the key hint).
        text = herdr.script(inst.instance, (agent,),
                            shell_cwd=WORKSPACE_IN_CONTAINER, solo=True)
    else:
        text = tmux.script(
            inst.instance, (agent,),
            shell_cwd=WORKSPACE_IN_CONTAINER,
            solo=True,
            # The HOST workspace, which is what the operator recognises; the
            # container-side cwd is `/workspace` for every instance alike.
            project_label=inst.workspace,
            # settings/tmux.conf, mounted read-only — sourced last so the
            # operator's overrides beat every default the script just set.
            user_conf=TMUX_CONF_IN_CONTAINER,
        )
    write_text(host, text)
    host.chmod(0o755)
    return container
