"""What `{muxer}` does to a SOLO launch: run the agent inside a multiplexer.

Without this, the tag only *installs* tmux — the container still hands the
terminal straight to `claude`, so nothing looks different and none of the tag's
promises (an extra shell, surviving a detach, one deliberate way out) are true.
This module is the difference between the tag being installed and the tag being
felt.

**How the command gets replaced.** The base image's `ENTRYPOINT` is `claude`, so
everything after the image name is claude's own argv. To run something else the
entrypoint has to be overridden — the mechanism `{firewall}` already uses. Rather
than ship a static shell script that would have to re-implement the tmux assembly
in a second language (and drift from it), the launcher GENERATES the startup
script from `tmux.solo_argv` into the instance's own state dir, which is already
bind-mounted into the container. One source of truth, and the exact script that
ran is left on disk afterwards for anyone debugging a launch.

**Composing with other wrappers.** `{muxer}` is the LAST link in
`docker_config.entrypoint_chain`: `{firewall}` (or any future wrapper) runs first,
does its job, and `exec "$@"`s this script, which then starts the multiplexer with
the agent baked in. Nothing follows it, which is why the agent's argv is baked
rather than passed through — see `install_launcher`.
"""

from __future__ import annotations

from pathlib import Path

from ..file_access import write_text
from ..paths import CLAUDE_CONFIG_IN_CONTAINER, WORKSPACE_IN_CONTAINER
from ..tags.identity import Instance
from . import tmux

SCRIPT_NAME = "muxer-start.sh"      # written into the instance state dir each launch
# What the container runs. Declared in `agents/specialty/muxer/tag.docker` too —
# that file is the one the launcher reads, this constant is what writes the file
# it names. test_cluster_solo pins them together.
CONTAINER_SCRIPT = str(CLAUDE_CONFIG_IN_CONTAINER / SCRIPT_NAME)


def script_paths(inst: Instance) -> tuple[Path, str]:
    """`(host path to write, container path to exec)` for this launch's script.

    Two paths for one file, like the cluster banner: the launcher writes it
    host-side into the state dir, and the container sees that dir mounted at
    CLAUDE_CONFIG_IN_CONTAINER. Deriving the container side from the SAME
    filename keeps them from drifting."""
    return inst.state_dir / SCRIPT_NAME, CONTAINER_SCRIPT


def install_launcher(inst: Instance, agent_argv: tuple[str, ...]) -> str:
    """Write this launch's tmux startup script; return the container path to run.

    `agent_argv` is the full command the container would otherwise have run
    (`claude` plus every flag the launcher assembled). It is baked into the
    script rather than passed through as `"$@"` because the script IS the
    entrypoint: quoting an argv through two shells is exactly where a
    `--append-system-prompt` containing spaces would come apart.
    """
    host, container = script_paths(inst)
    agent = tmux.Pane(name=tmux.AGENT_PANE, command=agent_argv,
                      cwd=WORKSPACE_IN_CONTAINER)
    write_text(host, tmux.script(
        inst.instance, (agent,),
        shell_cwd=WORKSPACE_IN_CONTAINER,
        solo=True,
        # The HOST workspace, which is what the operator recognises; the
        # container-side cwd is `/workspace` for every instance alike.
        project_label=inst.workspace,
    ))
    host.chmod(0o755)
    return container
