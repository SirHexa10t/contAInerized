"""herdr — the agent-native cluster backend (tmux's flagged sibling).

Everything here rests on a LIVE PROBE (2026-08-29, herdr v0.8.2, run inside a
launcher container), not on docs alone — the recorded adoption blockers were
each tested:

- **the server runs headless and owns the PTYs**: panes were created, driven,
  and read over the socket CLI with NO client ever attached, which retires the
  "untested as PID 1" risk — the entrypoint backgrounds `herdr server`, and
  detaching a client cannot take the members with it;
- **per-tab env works**: `tab create --env K=V --cwd … --label <member-id>`
  reached the tab's shell (echoed back from inside the pane), the same
  property that originally chose tmux — plus herdr injects `HERDR_ENV`,
  `HERDR_PANE_ID`, … so a member can drive its own multiplexer;
- **members are DETECTED and listed by name**: `agent start golem --kind
  claude --pane w1:p3` launched claude, and `agent list` reported it as
  `golem`, `agent_status: idle`, `interactive_ready: true` — the live roster
  (sidebar + API) that the tmux backend approximates with a banner file;
- **keys**: same `ctrl+b` prefix as tmux; `prefix+n`/`prefix+p` cycle member
  tabs, `prefix+1..9` jump, `prefix+b` toggles the sidebar, `prefix+?` is
  help — and DETACH IS `prefix+q` (not tmux's `d`), which the launch banner
  says out loud.

Like tmux.py, this module only ASSEMBLES: `script()` returns the container
entrypoint's shell text and nothing executes here. No banner parameter — the
sidebar and detection ARE the roster, natively; the file-based banner is a
tmux-ism. Config rides `settings/herdr.toml`, mounted read-only at herdr's
default path (`~/.config/herdr/config.toml`), so the user's file is the keys
policy exactly as tmux.conf is for tmux.
"""

from __future__ import annotations

import shlex
from pathlib import Path

from ..ai import active_adapter
from .member import valid_label
from .panes import SHELL_LABEL, Pane

BINARY = "herdr"
# `herdr status server` EXITS 0 WHETHER OR NOT the server runs — it is a
# report, not a probe (measured: "status: not running", rc=0). So liveness is
# the printed line, never the exit code; both loops below grep for this.
# Assuming tmux-like semantics here made the readiness gate pass before the
# server bound AND made the container immortal after `herdr server stop` —
# caught by executing the generated script against the real binary, which is
# why that execution is part of the definition of done for this module.
_RUNNING = 'grep -q "status: running"'
# How the generated script fishes the new tab's pane id out of `tab create`'s
# JSON reply (needed to aim `agent start` at it). A sed over one well-known key
# rather than a python -c: the reply is single-line NDJSON and the id is the
# first "pane_id" in it (the tab's root pane) — probed, not presumed.
_PANE_ID_SED = r"sed -n 's/.*\"pane_id\":\"\([^\"]*\)\".*/\1/p' | head -n 1"
# Same trick for the tab id — the root tab is renamed after the first agent.
# (The workspace id needs no sed: it is the pane id's prefix, w1:p1 → w1.)
_TAB_ID_SED = r"sed -n 's/.*\"tab_id\":\"\([^\"]*\)\".*/\1/p' | head -n 1"
# The server needs a beat to bind its socket before the CLI can talk to it.
_READY_TRIES = 50          # × 0.2s = a 10s ceiling before the launch fails loud
_STOP_HINT = "herdr server stop"   # THE deliberate way out — ends every member
# The key hint. Its HOME is the tab row's right corner (tab_bar_right in the
# configs) — the operator's call, and CONFIRMED rendering (screenshot,
# 2026-08-30). The tour that settled it: a shell greeting "doesn't belong in
# the user's shell area" (tried, reverted); the `$keys` sidebar row never
# drew despite the metadata token landing in `workspace list` (kept for
# herdrs that learn to); and the PANE LABEL — which DOES render, on the
# split frame (an earlier "draws nothing" reading was wrong) — must
# therefore stay a PLAIN name: hotkeys on the shell's frame were reported as
# clutter the moment the corner hint worked. The window title carries a copy
# for terminals that show one.
HINT_TOKEN = "keys"
HINT_TEXT = "alt+/ help · alt+q quit"


def _env_flags(pane: Pane) -> list[str]:
    """One `--env` per variable, key-sorted (the same determinism rule as
    `tmux._env_flags`, whose dialect is `-e KEY=VALUE`). Verified live to
    reach the new shell — and so the agent started in it."""
    return [flag for key in sorted(pane.env)
            for flag in ("--env", f"{key}={pane.env[key]}")]


def _workspace_create(pane: Pane, session: str) -> str:
    """The workspace line — and it carries the FIRST agent's cwd and env,
    because that agent lives in the workspace's ROOT pane.

    Why the root pane hosts an agent rather than the free shell: herdr has no
    `tab move` verb (checked, v0.8.2 — list/create/get/focus/rename/close
    only), so tab ORDER is creation order and the only way to put the shell
    rightmost is to create it last. That means the root tab must be spent on
    something else, and `workspace create --env` (which does exist) makes it
    able to host a member. Bonus: both shapes now read the same — root tab =
    first agent, shell tab last."""
    argv = [BINARY, "workspace", "create", "--cwd", str(pane.cwd),
            "--label", session, *_env_flags(pane)]
    return shlex.join(argv)


def _tab_create(pane: Pane) -> str:
    """The `tab create` line for one member: label = member id, cwd, its env.
    Never focused — the root tab (the first member) keeps the focus, so
    attach lands on a member rather than on the shell."""
    argv = [BINARY, "tab", "create", "--cwd", str(pane.cwd),
            "--label", pane.name, *_env_flags(pane), "--no-focus"]
    return shlex.join(argv)


def _start_line(pane: Pane) -> str:
    """Start `pane.command` in the pane whose id sits in `$PANE`.

    A `claude` command goes through `agent start` — that is what makes herdr
    DETECT the member (named in `agent list`, idle/working in the sidebar);
    `pane run` would launch the same process invisible to the agent layer.
    Anything else (a future non-claude member) falls back to `pane run`.

    Both forms carry an `|| echo` fallback, deliberately: the script runs
    under `set -eu` as PID 1, so without it one member failing to start
    (`agent start` TIMES OUT when its process exits before registering —
    observed executing this script against v0.8.2) would kill the container
    before the attach — every OTHER member with it, and nothing left to read.
    A warning plus an empty pane is inspectable; a dead container is not."""
    failed = shlex.join(["echo", f"warning: member {pane.name!r} did not "
                         f"start; its pane is there, empty"])
    harness = active_adapter()
    if pane.command[0] == harness.binary and harness.herdr_agent_kind is not None:
        start = [BINARY, "agent", "start", pane.name, "--kind", harness.herdr_agent_kind,
                 "--pane"]
        args = list(pane.command[1:])
        return (shlex.join(start) + ' "$PANE"'
                + (f" -- {shlex.join(args)}" if args else "")
                + f" >/dev/null || {failed}")
    return (f"{BINARY} pane run \"$PANE\" "
            f"{shlex.join(list(pane.command))} >/dev/null"
            f" || {failed}")


def _member_lines(pane: Pane) -> list[str]:
    """One NON-FIRST member's setup: create its tab, then start its process
    in the tab's root pane (id fished from the create reply). The first
    member rides the workspace's root pane instead — see
    `_workspace_create`."""
    return [f"PANE=$({_tab_create(pane)} | {_PANE_ID_SED})",
            _start_line(pane)]


def script(session: str, panes: tuple[Pane, ...], *,
           shell_cwd: Path,
           unset_env: tuple[str, ...] = (),
           setup_commands: tuple[str, ...] = ()) -> str:
    """The whole startup as a runnable `sh` script — the herdr twin of
    tmux.script. Order is load-bearing:

    1. env unsets and the caller's filesystem setup (same contract as tmux);
    2. `herdr server` backgrounded, then a readiness poll — the CLI speaks to
       the socket, so racing it loses; a server that never comes up fails the
       launch loudly (10s ceiling) rather than assembling into the void;
    3. one workspace (label = the session), whose ROOT tab hosts the FIRST
       agent — renamed after it, since herdr's default "1" reads as nothing.
       Every other agent follows as its own tab, and the free shell is the
       LAST tab, so it sits rightmost. ONE shape for solo and cluster alike:
       a solo launch is simply this with one agent. (A solo instance briefly
       also had the shell as a SPLIT beneath its agent — the tmux layout,
       translated — and the operator asked for the tab only, 2026-09-03: an
       extra pane at the bottom of every screen was noise once the tab
       existed.);
    4. attach (`herdr` = attach-or-launch the default persistent session),
       then HOLD the container while the server lives: detaching (prefix+q)
       must leave everything running, exactly the tmux-path contract, and
       `herdr server stop` is the one deliberate way out.
    """
    valid_label(session, "session name")
    if not panes:
        raise ValueError("a cluster needs at least one member pane")
    lines = ["#!/bin/sh",
             "# Generated by launch.cluster.herdr — one tab per member "
             "(herdr backend).",
             "# Rewritten on every launch; left here so a failed start can "
             "be read.",
             "set -eu", ""]
    lines += [f"unset {name}" for name in unset_env]
    if unset_env:
        lines.append("")
    lines += list(setup_commands)
    if setup_commands:
        lines.append("")
    lines += [
        f"{BINARY} server >/dev/null 2>&1 &",
        'i=0',
        f"until {BINARY} status server 2>/dev/null | {_RUNNING}; do",
        f'  i=$((i+1)); [ "$i" -gt {_READY_TRIES} ] && '
        f'{{ echo "herdr server did not come up"; exit 1; }}',
        "  sleep 0.2",
        "done",
        "",
    ]
    hint = (shlex.join([BINARY, "workspace", "report-metadata"]) + ' "$WS" '
            + shlex.join(["--source", "launcher", "--token",
                          f"{HINT_TOKEN}={HINT_TEXT}"])
            + " >/dev/null || :")   # a lost hint must not kill PID 1
    # ONE root-tab path for both shapes: the workspace's root pane hosts the
    # FIRST agent (its cwd + env ride `workspace create`), and its tab is
    # renamed after it. The remaining members follow as tabs, and the free
    # shell is created LAST so it sits rightmost — herdr has no `tab move`,
    # so creation order IS tab order (operator request, 2026-09-02).
    first = panes[0]
    lines += [
        # The create reply is consumed twice (pane id AND tab id), so it is
        # captured whole rather than piped away.
        f"REPLY=$({_workspace_create(first, session)})",
        f'PANE=$(printf %s "$REPLY" | {_PANE_ID_SED})',
        f'TAB=$(printf %s "$REPLY" | {_TAB_ID_SED})',
        # The workspace id is the pane id's prefix (w1:p1 → w1).
        'WS="${PANE%%:*}"',
        hint,
        # The tab row is the key hint's surface (tab_bar_right rides it), so
        # every tab earns a real name rather than herdr's default "1" — a
        # cluster whose shell tab read "1" was reported as having no shell.
        shlex.join([BINARY, "tab", "rename"]) + ' "$TAB" '
        + shlex.join([first.name]) + " >/dev/null || :",
    ]
    lines.append(_start_line(first))
    for pane in panes[1:]:
        lines += _member_lines(pane)
    # The free shell, LAST and so rightmost — the operator's terminal in a
    # solo launch, the team's shared one in a cluster.
    lines.append(
        shlex.join([BINARY, "tab", "create", "--cwd", str(shell_cwd),
                    "--label", SHELL_LABEL, "--no-focus"])
        + " >/dev/null || :")
    lines += [
        "",
        f'{BINARY} || echo "could not attach to herdr (no TTY?)"',
        "",
        "# Detached, not finished: hold the container open while the server "
        "lives.",
        f'echo "detached — re-attach with:  docker exec -it $(hostname) '
        f'{BINARY}"',
        f'echo "(\'{_STOP_HINT}\' from any pane ends the whole cluster '
        f'and this container)"',
        f"while {BINARY} status server 2>/dev/null | {_RUNNING}; do sleep 2; done",
        "",
    ]
    return "\n".join(lines)
