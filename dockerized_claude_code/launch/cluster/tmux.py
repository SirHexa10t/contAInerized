"""The multiplexer layer: N members, one window, tmux.

tmux was chosen over zellij / WezTerm / herdr on researched grounds (full
comparison in the closed `multiplexer_research` group's `multiplexer.md`; verdict
recorded in `cluster_plan.md`). The two deciding properties both show up in this
module:

- **per-window environment is native.** `new-window -e KEY=VALUE` sets a
  variable for that window's process only, so each member's model/effort env is
  a first-class argument with no shell quoting. zellij has no equivalent node
  and would need `sh -c` wrapping per member.
- **the status line is shell-interpolated.** `#(command)` runs on an interval,
  so the banner listing the members is a file we write and tmux renders — we
  keep ownership of the content, which matters because the launcher already
  knows each member's state and tmux never will.

**Everything here is pure argv assembly.** Nothing executes; `startup_argv`
returns the exact command sequence and `script` renders it as a shell file for a
container entrypoint. That is deliberate — the assembly is the part with rules
worth testing (ordering, quoting, target naming), and it stays verifiable on a
machine with no tmux installed.

**Naming discipline.** Windows are named by member id, and every later command
addresses them as `<session>:<member-id>`. tmux target syntax is
`session:window.pane`, which is exactly why `member.valid_label` rejects `:` and
`.` — an id containing either would resolve to the wrong target rather than
error. The session name gets the same treatment.
"""

from __future__ import annotations

import shlex
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

from .member import valid_label

# The session lives on its OWN server socket, not tmux's default one.
#
# WHY, measured the hard way: an agent running inside the session shares a socket
# with it, and `$TMUX` points a bare `tmux` command at the server that owns the
# pane — so `tmux kill-server`, issued for any innocent reason, destroys the
# session hosting the agent AND the container with it (the startup script is PID
# 1). That happened twice to this very session while its own author was testing
# tmux. The addendum said "do not tear the session down"; advice is not a
# safeguard.
#
# With a named socket plus `unset TMUX` in the agent's pane (see `solo_argv`), a
# careless `tmux kill-server` hits an EMPTY scratch server and is harmless, while
# touching the real session requires naming it: `tmux -L muxer …`. Destructive by
# accident becomes destructive only on purpose.
#
# `-u` on every invocation because the container sets no LANG/LC_ALL/LC_CTYPE
# (verified), so tmux cannot infer a UTF-8 locale and falls back to byte handling
# that mangles wide glyphs — Claude Code's icon rendered as a black box and its `❯`
# prompt as an underscore, while the same session outside tmux was fine.
SOCKET = "muxer"
TMUX = ("tmux", "-u", "-L", SOCKET)
BANNER_REFRESH_SECONDS = 5      # how often tmux re-runs the status-right command
SHELL_WINDOW = "shell"          # the free terminal, always last
SHELL_COMMAND = ("bash", "-l")  # login shell: the operator's bashrc/aliases apply
# The interactive KEY POLICY — quit, help, layout, mouse behavior — is NOT
# assembled here: it ships as active lines in `settings/tmux.conf`, which the
# generated script sources LAST, so the same file is both the defaults and the
# user's override surface (edit a line, done — no Python). This module keeps
# what a static file cannot express: session construction, computed content
# (labels, banner), generated bindings (type-through), and the plumbing whose
# correctness the launcher owes (mode-keys pin, clipboard emission).
#
# These two key names still live here because the STATUS-BAR HINT renders them
# ("ctrl+b, shift+q": quit) while the bindings themselves are conf lines — a
# test pins the conf to these constants so the hint cannot drift into a lie.
KILL_KEY = "Q"                  # prefix + this, with a confirmation, ends everything
HELP_KEY = "?"                  # the curated help popup (settings/muxer-help.txt)
COPY_TABLE = "copy-mode"        # the key table tmux dispatches through while
                                # scrolled back — the reason typing was swallowed
# What "typing" means: every printable ASCII character. Bound one by one, because
# tmux's scroll-back view eats every one of them that is not — see
# `_typethrough_command`.
_PRINTABLE = tuple(chr(code) for code in range(0x20, 0x7F))
# The type-through batch renders as one ~6KB line of near-identical bindings. This
# script is kept so a failed start can be READ, so that line gets a label telling
# the reader they can skip it.
TYPETHROUGH_LABEL = ("# Any printable key leaves the scroll-back view and lands in"
                     " the pane. One call: 95 would cost 0.7s of spawning.")

AGENT_PANE = "agent"            # pane title for the window the agent owns
SHELL_PANE_PERCENT = 22         # how much height the free shell gets in a solo split
# Seconds to wait before re-applying the split after a terminal resize. MEASURED
# (tmux 3.5a): the `client-resized` hook fires BEFORE tmux finishes re-laying the
# window out, so an immediate `resize-pane` is undone. Deferring by this much
# holds the ratio across grow and shrink; without it the ratio drifts every time.
RELAYOUT_DELAY = 0.4
# Styling kept minimal and 256-colour-safe: a cluster is identified on the left,
# members are listed as windows in the middle, our own banner file on the right.
_STATUS_STYLE = "bg=colour236,fg=colour252"
_CURRENT_STYLE = "bg=colour31,fg=colour231,bold"


@dataclass(frozen=True)
class Pane:
    """One member's window: its name, what to run, where, and with what env.

    `command` is an argv TUPLE rather than a string because the caller thinks in
    arguments; it is shell-quoted exactly once, here, when tmux needs it as a
    single `shell-command` word."""
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
        """`command` as the one shell word tmux runs.

        `shlex.join` rather than `" ".join`: a member's command can carry an
        argument with a space (a `--append-system-prompt`, a path), and tmux
        hands this string to a shell, so unquoted joining would split it."""
        return shlex.join(self.command)

    def env_flags(self) -> tuple[str, ...]:
        """`-e KEY=VALUE` pairs, key-sorted so the assembly is deterministic and
        a test can assert on it."""
        flags: list[str] = []
        for key in sorted(self.env):
            flags += ["-e", f"{key}={self.env[key]}"]
        return tuple(flags)


def shell_pane(cwd: Path, env: Mapping[str, str] | None = None) -> Pane:
    """The free terminal every `{muxer}` container gets.

    This is the tag's standalone value, not a cluster detail: a window the agent
    does not own, for logging into a CLI, fixing bashrc, tailing a log, checking
    the network, or running a server beside the work. A login shell rather than
    plain `bash` so the operator's own profile applies."""
    return Pane(name=SHELL_WINDOW, command=SHELL_COMMAND, cwd=cwd,
                env=env or {})


def solo_argv(session: str, agent: Pane, *, shell_cwd: Path,
              project_label: str | None = None,
              banner: Path | None = None,
              refresh: int = BANNER_REFRESH_SECONDS,
              shell_percent: int = SHELL_PANE_PERCENT,
              ) -> tuple[tuple[str, ...], ...]:
    """A SOLO `{muxer}` instance: one window, two panes — the agent on top, a
    free shell beneath it, both visible at once.

    This is what makes the tag felt rather than merely installed. A cluster
    switches between members (one window each, full height, because a member's
    TUI needs the room); a solo instance has only one agent, so the second pane
    can simply be on screen the whole time.

    **Stacked, not side by side.** `split-window` defaults to a horizontal
    divider, which is what we want: Claude Code's output is width-sensitive (code
    blocks and diffs wrap badly in half a terminal), whereas a shell is perfectly
    usable in a few rows. So the agent keeps the full width and gives up
    `shell_percent` of the height.

    **Consequence worth knowing:** with the agent inside tmux, quitting it no
    longer ends the container — the session outlives it, deliberately, so the
    shell survives to poke at whatever the agent left behind. The way out is the
    same advertised binding as in a cluster (prefix + Q).

    `project_label` is what appears beside the instance name in the status bar:
    the HOST workspace path, passed in because this module cannot know it (the
    agent's cwd is the container-side mount).

    `banner` defaults to None here, unlike the cluster path. A cluster's banner
    earns its place (member count, later who is mid-turn); a solo instance has
    nothing to say there that the left label and Claude Code's own statusline do
    not already say, and the first render duplicated the instance name on both
    sides of the bar. The parameter stays for when there IS dynamic state worth
    showing.
    """
    valid_label(session, "session name")
    if not 1 <= shell_percent <= 90:
        raise ValueError(f"shell_percent must be 1-90, got {shell_percent}")
    shell = shell_pane(shell_cwd)
    # `unset TMUX` ONLY in the agent's pane. tmux points a bare `tmux` command at
    # the server that owns the pane via `$TMUX`, so without this the agent's own
    # multiplexer experiments run against the session hosting it — one
    # `kill-server` and both the session and the container are gone. Removing the
    # variable sends the agent's bare `tmux` to an empty default socket instead.
    # The OPERATOR's shell keeps `$TMUX`: a human in that pane should be able to
    # drive their own session without knowing the socket name.
    caged = f"unset TMUX; exec {agent.shell_command}"
    commands: list[tuple[str, ...]] = [
        (*TMUX, "new-session", "-d", "-s", session, "-n", agent.name,
         "-c", str(agent.cwd), *agent.env_flags(), "sh", "-c", caged),
    ]
    # Same session-environment scrub as the cluster path, for the same measured
    # reason (`new-session -e` leaks into the session env). Harmless here even
    # when there is nothing to scrub, and it keeps one rule: a window's
    # environment is exactly what that window was given.
    commands += [(*TMUX, "set-environment", "-t", session, "-u", key)
                 for key in sorted(agent.env)]
    commands.append(
        (*TMUX, "split-window", "-t", f"{session}:{agent.name}", "-v",
         "-l", f"{shell_percent}%", "-c", str(shell.cwd), shell.shell_command))
    # Name the panes so the divider says which is which — with only two panes a
    # thin label is cheaper than making the user guess.
    commands += [
        (*TMUX, "select-pane", "-t", f"{session}:{agent.name}.0", "-T", AGENT_PANE),
        (*TMUX, "select-pane", "-t", f"{session}:{agent.name}.1", "-T", SHELL_WINDOW),
    ]
    commands += _ratio_hooks(session, f"{session}:{agent.name}.1", shell_percent)
    # The label carries the project, matching the launch banner's shape. It must
    # be the HOST path the operator recognises — the agent's cwd is the container
    # mount (`/workspace`), which is the same string for every instance and so
    # tells the reader nothing.
    label = (f"{session} ( {project_label} )" if project_label else session)
    commands += _option_argv(session, banner=banner, refresh=refresh,
                             pane_titles=True, show_windows=False,
                             left_label=label)
    # Land on the agent, not the shell that was created last.
    commands.append((*TMUX, "select-pane", "-t", f"{session}:{agent.name}.0"))
    return tuple(commands)


def startup_argv(session: str, panes: tuple[Pane, ...], *,
                 banner: Path | None = None,
                 refresh: int = BANNER_REFRESH_SECONDS,
                 shell_cwd: Path | None = None,
                 ) -> tuple[tuple[str, ...], ...]:
    """The full command sequence that builds the cluster's tmux session.

    Order is load-bearing, not stylistic:

    1. `new-session -d` creates the session AND its first window in one call —
       tmux has no way to make an empty session, so the first member is special
       whether we like it or not.
    2. `set-environment -u` for each of that first member's variables, undoing
       the session-wide leak `-e` causes on this one call (see the comment at the
       call site — this was measured, not assumed).
    3. `new-window` per remaining member, in definition order, so window numbers
       follow the template's order.
    4. options last, because `set-option -t <session>` needs the session to
       exist.
    5. `select-window` back to the first member, so the user lands on the
       intended member rather than the last one created.

    `shell_cwd` adds the free terminal as a final window (see `shell_pane`);
    passing None omits it, which is what a caller wanting members only would do.

    Attaching is NOT included — see `attach_argv`. It blocks, and the caller
    needs to decide whether it is exec'ing into it or running it in a container.
    """
    valid_label(session, "session name")
    if not panes:
        raise ValueError("a cluster tmux session needs at least one member")
    if shell_cwd is not None:
        panes = (*panes, shell_pane(shell_cwd))
    first, *rest = panes
    commands: list[tuple[str, ...]] = [
        (*TMUX, "new-session", "-d", "-s", session, "-n", first.name,
         "-c", str(first.cwd), *first.env_flags(), first.shell_command),
    ]
    # MEASURED (tmux 3.5a): `new-session -e` writes the variable into the
    # SESSION environment, not just the first window's process — so every window
    # created afterwards WITHOUT that variable inherits the first member's value.
    # The free shell window reported `CLUSTER_MEMBER=<first member>`, i.e. the
    # operator's own shell claimed to be a cluster member. `new-window -e` does
    # not do this; only the session-creating call does.
    #
    # Scrubbing the session environment straight afterwards fixes it with no
    # placeholder-window dance: the first member's process has already exec'd and
    # keeps its own copy (verified), while later windows see nothing.
    commands += [(*TMUX, "set-environment", "-t", session, "-u", key)
                 for key in sorted(first.env)]
    commands += [
        (*TMUX, "new-window", "-t", session, "-n", pane.name,
         "-c", str(pane.cwd), *pane.env_flags(), pane.shell_command)
        for pane in rest
    ]
    commands += _option_argv(session, banner=banner, refresh=refresh)
    commands.append((*TMUX, "select-window", "-t", f"{session}:{first.name}"))
    return tuple(commands)


def _ratio_hooks(session: str, target: str, percent: int
                 ) -> list[tuple[str, ...]]:
    """Hooks that keep the split at `percent` whatever the terminal does.

    **This exists because of a measured bug, not a hypothetical one.** A session
    is created detached — the entrypoint has no client yet — so its window starts
    at tmux's default 80x24, and `split-window -l 22%` sizes the panes against
    THAT. When a client then attaches at a real terminal size, tmux distributes
    the new rows **equally between the panes rather than proportionally**:
    measured on 3.5a, growing 24 → 58 rows added 17 rows to each pane, turning a
    22% shell into 39%. Asking for 15% and asking for 22% both ended up near 35%
    on a tall terminal, i.e. the requested number stopped meaning anything.

    So the ratio is re-applied on `client-attached` (the launch case, and every
    later re-attach from a different terminal) and on `client-resized` (the user
    dragging their window). The resize case must be DEFERRED — see
    RELAYOUT_DELAY.
    """
    resize = f"resize-pane -t {target} -y {percent}%"
    # `" ".join(TMUX)` — NOT `{TMUX}`. This one is a SHELL command string rather
    # than an argv element, so formatting the tuple directly wrote its Python repr
    # into the hook: `sleep 0.4; ('tmux', '-u') resize-pane …`, which the shell
    # cannot run (exit 2) and which tmux then reported into the agent's pane on
    # every resize. Caught from a screenshot of a real launch, not by a test —
    # hence the one below.
    return [
        (*TMUX, "set-hook", "-t", session, "client-attached", resize),
        (*TMUX, "set-hook", "-t", session, "client-resized",
         f'run-shell -b "sleep {RELAYOUT_DELAY}; {" ".join(TMUX)} {resize}"'),
    ]


def _key_token(char: str) -> str:
    """`char` as tmux spells it in the KEY position of `bind-key`.

    Two exceptions, both measured on 3.5a against every printable character:

    * a space is the key NAME `Space`. An argument that looks like whitespace is
      not a key.
    * `;` must arrive ESCAPED. tmux reads a lone `;` argv element as a command
      separator, so the unescaped form ends `bind-key` early and it reports "too
      few arguments (need at least 1)" — the one character out of 95 that fails
      silently enough to be missed.
    """
    if char == " ":
        return "Space"
    return "\\;" if char == ";" else char


def _send_command(char: str) -> str:
    """The tmux command that puts `char` into the pane's process.

    `-l` (literal) rather than a key name, so `#`, `$` and `~` arrive as
    themselves rather than being looked up as key names. The character is QUOTED
    because this string is parsed by tmux's own lexer, where `;` separates
    commands and `"` and `#` are special; single quotes cover every character
    except a single quote, which takes double ones.
    """
    if char == " ":
        # `-l " "` would hand tmux a whitespace-only argument; the name form is
        # unambiguous and verified to deliver 0x20.
        return "send-keys Space"
    quote = '"' if char == "'" else "'"
    return f"send-keys -l {quote}{char}{quote}"


def _typethrough_command() -> tuple[str, ...]:
    """Stop the scroll-back view from swallowing what the operator types.

    **The reported bug, and it was ours.** `mouse on` leaves tmux's own wheel
    binding in place: `copy-mode -e`, which means scrolling up puts the pane into
    copy-mode, where keys are dispatched through the `copy-mode` KEY TABLE instead
    of being sent to the process. Of the 95 printable characters, 81 are unbound
    there and silently dropped; the other 14 do something unrelated (`q` leaves,
    `Space` pages down, `g` opens a goto-line prompt). Hence the symptom: scroll up
    in Claude Code, type, and nothing appears until you scroll back to the bottom,
    which is where `-e` quietly exits the mode.

    A plain terminal has no such state — you type while scrolled up, it jumps to
    the bottom, your character is in the prompt. These bindings reproduce exactly
    that: cancel the view, then deliver the character. Order is what makes it work,
    and it was verified rather than assumed — a pane in copy-mode given
    `send -X cancel ; send-keys -l Z` left the mode AND the application received
    the `Z`.

    **Deliberate cost.** The 14 letter-keys copy-mode ships lose those meanings
    everywhere, not just after a scroll, because a key table cannot tell how the
    mode was entered. Arrows / PageUp / Home / End still navigate, `C-Space` and
    `M-w` still select and copy, `C-r` / `C-s` still search (and their prompts
    still take letters, being a command-prompt rather than this table), and
    `Escape` still leaves. What goes is vi-flavoured letter navigation — a fair
    trade in a session whose main pane is a prompt people type prose into, and the
    reason the popup now says "Escape to leave" where it said "press q".

    **One tmux call, not 95.** Measured: 95 separate `bind-key` invocations spend
    0.72s on process spawning at container start, for bindings that are identical
    every launch. Chaining them with `;` argv separators is one client connection,
    and it keeps the generated script to a single labelled line.
    """
    chained: list[str] = []
    for char in _PRINTABLE:
        if chained:
            chained.append(";")
        chained += ["bind-key", "-T", COPY_TABLE, _key_token(char),
                    f"send -X cancel ; {_send_command(char)}"]
    return (*TMUX, *chained)


def _option_argv(session: str, *, banner: Path | None, refresh: int,
                 pane_titles: bool = False, left_label: str | None = None,
                 show_windows: bool = True) -> list[tuple[str, ...]]:
    """The session's options — the LAUNCHER-OWED half of the configuration:
    computed content (labels, banner, lengths) and correctness plumbing. Taste
    lives in settings/tmux.conf instead (`mouse on`, every interactive key),
    where the user edits it directly.

    `remain-on-exit on` is the one worth justifying: without it, a member whose
    `claude` dies takes its window with it, so a crash looks like a member that
    was never started. With it the pane stays, showing the exit status — the
    difference between a diagnosable cluster and a confusing one. It stays
    Python-side as a diagnosability invariant, not a preference.
    """
    options: list[tuple[str, str]] = [
        # `mouse on` is NOT here — it is the first active line of
        # settings/tmux.conf, the canonical example of policy belonging to the
        # user's file.
        # PINNED, not left to tmux's default, which is derived from $EDITOR /
        # $VISUAL at server start: with `vi` in either, copy-mode dispatches
        # through the `copy-mode-vi` table instead, and the type-through bindings
        # below — which target `copy-mode` — would silently not apply. One line
        # here beats emitting both tables, and the letter keys those tables differ
        # over are exactly the ones type-through repurposes anyway.
        ("mode-keys", "emacs"),
        # `on` rather than the `external` default. Both attempt the terminal
        # clipboard when tmux itself copies; `on` additionally accepts OSC 52 from
        # an application inside a pane. Stating it removes a default we do not
        # control from the path between a mouse drag and the operator's clipboard.
        ("set-clipboard", "on"),
        ("remain-on-exit", "on"),              # a dead member stays visible
        ("status", "on"),
        ("status-interval", str(refresh)),
        ("status-style", _STATUS_STYLE),
        # A solo instance is not a cluster and must not claim to be one — the
        # first render of the solo split said "cluster:<name>", which is exactly
        # the kind of wrong label nobody reads twice.
        ("status-left",
         f" #[bold]{left_label if left_label is not None else f'cluster:{session}'}#[default] "),
        # Sized to the label, not a guess: a solo instance's label carries the
        # host project path, which is routinely longer than the 40 columns that
        # sufficed for `cluster:<name>` — and tmux silently TRUNCATES rather than
        # complaining, so a too-short value looks like a wrong label.
        ("status-left-length",
         str(max(40, len(left_label or f"cluster:{session}") + 6))),
        # The member list: tmux renders one entry per window, so this IS the
        # "banner lists the available agents" requirement — for a CLUSTER. A solo
        # instance has one window, so the list would just repeat itself.
        ("window-status-format", " #W " if show_windows else ""),
        ("window-status-current-format",
         f" #[{_CURRENT_STYLE}] #W #[default] " if show_windows else ""),
        # 120, up from 90: the key hints are spelled all the way out now
        # (~56 cells), and a cluster's banner rides in front of them — tmux
        # TRUNCATES the overflow silently, and the tail is the quit hint,
        # the one line a newcomer most needs.
        ("status-right-length", "120"),
        # Our own content, re-read every `refresh` seconds. A missing file must
        # not print an error into the status bar, hence the `2>/dev/null ||:`.
        # The hints ride the status line, because a binding nobody can see is a
        # binding nobody uses — and one of these is the only clean way out of the
        # session, which a user who has never met tmux would otherwise have to
        # guess. Spelled ALL the way out ("ctrl+b, shift+q"), unlike the compact
        # `^b` inside the popup: the corner is the entry point for someone who
        # knows no tmux notation yet, and it must be executable without a
        # legend — the popup it leads to is where the shorthand gets taught.
        # `shift+` on both, though `?` implies it: the quit key is where the
        # case genuinely bites (lowercase q is tmux's display-panes overlay,
        # met as "weird red/blue numbers" in a live session), and one spelling
        # rule for both reads cleaner than two.
        ("status-right",
         (f"#({shlex.join(('cat', str(banner)))} 2>/dev/null ||:)"
          if banner is not None else "")
         + f' #[dim]"ctrl+b, shift+{HELP_KEY}": help  |  '
           f'"ctrl+b, shift+{KILL_KEY.lower()}": quit#[default] '),
    ]
    if pane_titles:
        # Only the solo split shows these: in a cluster each window IS one
        # member, so a per-pane label would repeat the window name.
        options += [
            ("pane-border-status", "top"),
            ("pane-border-format", " #{pane_title} "),
        ]
    commands: list[tuple[str, ...]] = [
        (*TMUX, "set-option", "-t", session, "-g", name, value)
        for name, value in options]
    # Clipboard plumbing: assert the `clipboard` feature for every terminal, so
    # tmux emits OSC 52 on copy regardless of how current the client's terminfo
    # is. `-as` because terminal-features is a server option holding a LIST —
    # assigning would drop tmux's own per-terminal entries. Plumbing, not
    # policy, which is why it stays here rather than in settings/tmux.conf:
    # whether a copy is OFFERED to the terminal is the launcher's correctness
    # concern; what the keys and the mouse DO with a copy is the user's file.
    commands.append((*TMUX, "set-option", "-as", "terminal-features",
                     ",*:clipboard"))
    # Type-through, emitted for BOTH shapes: a cluster member's pane swallows
    # keystrokes exactly like a solo agent's, because the fault is in a key
    # table — server-global — not in the layout. Generated (95 bindings), so it
    # could never live in the static conf; its `mode-keys emacs` pin rides in
    # the options above for the same reason — the two must move together.
    commands.append(_typethrough_command())
    # NOT here, deliberately: the quit / help / layout / mouse bindings. Those
    # are user-facing key POLICY and ship as active lines in settings/tmux.conf
    # (sourced last by `script`), so changing a key is editing a config file,
    # not this module. The status-right hint above names two of them — the
    # KILL_KEY / HELP_KEY constants exist for that hint, and a test pins the
    # conf's bindings to the same letters so the hint cannot drift into a lie.
    return commands


def attach_argv(session: str) -> tuple[str, ...]:
    """The blocking attach. Separate from `startup_argv` because it is the point
    where the terminal changes hands, and because a container entrypoint attaches
    while a host-side caller may instead `docker exec` into it."""
    valid_label(session, "session name")
    return (*TMUX, "attach-session", "-t", session)


def kill_argv(session: str) -> tuple[str, ...]:
    """Tear the whole session down — every member's process with it."""
    valid_label(session, "session name")
    return (*TMUX, "kill-session", "-t", session)


def banner_text(members: tuple[str, ...], *, project: str | None = None) -> str:
    """What the status-right file holds today: how many members, and the project.

    Deliberately thin. The interesting version of this line — who is mid-turn,
    who is blocked, unread counts — needs a source of that state, which is the
    launcher's (or later the queue's) job, not tmux's. Keeping the renderer a
    plain `cat` of a file means that upgrade is a change of writer, not of
    multiplexer."""
    parts = [f"{len(members)} member(s)"]
    if project:
        parts.append(project)
    return " · ".join(parts)


def script(session: str, panes: tuple[Pane, ...], *, banner: Path | None = None,
           unset_env: tuple[str, ...] = (),
           setup_commands: tuple[str, ...] = (),
           shell_cwd: Path | None = None,
           solo: bool = False,
           project_label: str | None = None,
           user_conf: Path | None = None) -> str:
    """The whole startup as a runnable `sh` script — what a container entrypoint
    executes.

    `unset_env` exists for one specific, researched reason: cross-session
    messaging dies if feature-flag evaluation is disabled, and
    `CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC` is **sticky** — setting it to `0`
    still disables, so the variable has to be UNSET before `claude` starts. The
    base image sets it, so a cluster that ever wants messaging has to undo it
    here, in the one place that runs before every member. Empty by default:
    PoC-0 has no messaging and keeps the image's privacy posture.

    `set -eu` because a half-built tmux session is worse than a failed launch —
    the user would be looking at a cluster missing members with no error.

    `user_conf` names the operator's tmux config (settings/tmux.conf, mounted
    read-only), sourced LAST — after everything assembled above — for two
    reasons at once: it CARRIES the interactive key policy (quit, help,
    layout, mouse behavior — those bindings exist nowhere in this module), and
    anything else the user sets there wins over the launcher's options. `-q`
    so a deleted conf degrades the session (no key bindings) rather than
    killing the launch under `set -eu`; the mount ships the file with every
    container, so that case is deliberate user action, not an accident.
    """
    shape = ("the agent plus a free shell, split in one window" if solo
             else "one window per member")
    lines = ["#!/bin/sh",
             f"# Generated by launch.cluster.tmux — {shape}.",
             "# Rewritten on every launch; left here so a failed start can be read.",
             "set -eu", ""]
    lines += [f"unset {name}" for name in unset_env]
    if unset_env:
        lines.append("")
    # Raw shell lines the caller needs run before the multiplexer exists — the
    # cluster launch uses these for its per-member filesystem setup (the shared
    # sessions/ symlinks and the config-dir plumbing). Verbatim: the caller owns
    # their quoting, and they run under this script's `set -eu`, so a failed
    # setup stops the launch instead of producing half-wired members.
    lines += list(setup_commands)
    if setup_commands:
        lines.append("")
    # `solo` picks the one-window split (agent + free shell, both on screen);
    # otherwise it is the cluster shape, one window per member.
    if solo:
        if len(panes) != 1:
            raise ValueError(f"a solo session has exactly one agent pane, got {len(panes)}")
        if shell_cwd is None:
            raise ValueError("a solo session needs shell_cwd for its free terminal")
        assembly = solo_argv(session, panes[0], shell_cwd=shell_cwd,
                             project_label=project_label, banner=banner)
    else:
        assembly = startup_argv(session, panes, banner=banner, shell_cwd=shell_cwd)
    typethrough = _typethrough_command()
    for argv in assembly:
        if argv == typethrough:
            lines.append(TYPETHROUGH_LABEL)
        lines.append(shlex.join(argv))
    if user_conf is not None:
        lines += ["",
                  "# The operator's own overrides — sourced last, so they win.",
                  shlex.join((*TMUX, "source-file", "-q", str(user_conf)))]
    # `|| echo` rather than a bare attach: under `set -e` a failed attach would
    # exit the script — i.e. stop the container — before the wait loop below ever
    # ran, with nothing said about why. Measured: attaching with no TERM set fails
    # exactly this way.
    lines += ["",
              f"{shlex.join(attach_argv(session))} || "
              f'echo "could not attach to the session (no TTY?)"']
    # This script is the container's PID 1, so when `attach` RETURNS the container
    # would stop — taking the tmux server and every pane with it. That makes
    # `prefix d` (detach) destructive, which is the opposite of what the tag
    # promises. Waiting while the session still exists separates the two
    # intentions: detaching leaves everything running and re-attachable, while
    # `prefix Q` ends the session and so ends this script and the container.
    lines += [
        "",
        "# Detached, not finished: hold the container open while the session lives.",
        f'echo "detached — re-attach with:  docker exec -it $(hostname) '
        f'tmux -L {SOCKET} attach -t {shlex.quote(session)}"',
        'echo "(this terminal can be closed; Ctrl-C here stops the container)"',
        f'while {" ".join(TMUX)} has-session -t {shlex.quote(session)} 2>/dev/null; '
        f"do sleep 2; done",
        "",
    ]
    return "\n".join(lines)
