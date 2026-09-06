"""What a multiplexer is asked to start — the vocabulary BOTH backends speak.

One `Pane` per thing that gets its own view: each cluster member, and a solo
launch's agent. `launch_plan` builds them from a `Cluster`, and whichever
backend is selected (`herdr` by default, `tmux` behind the ui-profile toggle)
renders them into its own startup script.

Split out of `tmux.py` 2026-09-03. The record was defined there, so the
DEFAULT backend imported its vocabulary from the fallback one — `herdr.py`
opened with `from .tmux import SHELL_LABEL, Pane`, and `launch_plan` and
`solo` took `Pane` from there too. Nothing about the record is tmux's: it
carries a name, an argv, a cwd and an env, and that is what both backends
need to know.

What deliberately did NOT come along: the `-e KEY=VALUE` rendering. That is
tmux's flag syntax and herdr spells the same idea `--env KEY=VALUE`, so each
backend formats the env itself (`tmux._env_flags` / `herdr._env_flags`) from
the neutral mapping this record holds. A formatter on the shared record would
be one backend's dialect in the shared layer, which is how the split got
crooked the first time.
"""

from __future__ import annotations

import shlex
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

from .member import valid_label

# The two labels neither backend owns. Both are user-visible: they name the
# window/tab the operator switches to, so they read as words, not ids.
AGENT_PANE = "agent"            # the pane a SOLO launch's agent owns
SHELL_LABEL = "shell"           # the free terminal every {muxer} gets, always last


@dataclass(frozen=True)
class Pane:
    """One member's window: its name, what to run, where, and with what env.

    `command` is an argv TUPLE rather than a string because the caller thinks in
    arguments; it is shell-quoted exactly once, at the point a backend needs it
    as a single shell word."""
    name: str
    command: tuple[str, ...]
    cwd: Path
    env: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        valid_label(self.name, "window name")
        if not self.command:
            raise ValueError(f"window {self.name!r} has no command to run")

    @property
    def shell_command(self) -> str:
        """`command` as the one shell word a backend runs.

        `shlex.join` rather than `" ".join`: a member's command can carry an
        argument with a space (a `--append-system-prompt`, a path), and the
        backends hand this string to a shell, so unquoted joining would split
        it."""
        return shlex.join(self.command)
