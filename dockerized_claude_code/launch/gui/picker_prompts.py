"""The picker's NON-fullscreen interaction: line prompts, inline dialogs, and
the field builders its forms are handed (launch/gui).

Split out of menu_picker 2026-09-03 with `picker_flows`, which left that
module the picker WIDGET (rows, previews, the selection loop) and nothing
else. These live one layer down because BOTH sides need them — a flow asks
`confirm_dialog` before destroying a cluster, and the picker asks it before
deleting an instance; a flow reports a stale row with `_report_to_picker`,
and so does the selection loop.

  confirm_dialog / _report_to_picker   inline yes/no + "explain, then pause"
                                       (the pause matters: the picker's
                                       redraw would wipe the explanation)
  ask_for_workspace                    the path prompt, with completion
  _agent_description / _agent_rows     an agent's one-liner, and the agent
                                       list every membership form is given
  *_field_error / *_fields             live validators + the TextField rows
                                       the instance and cluster forms carry

Leaf within the flow chain: `picker_prompts` → `picker_flows` →
`menu_picker`, strictly one way.
"""

from __future__ import annotations

import readline
from pathlib import Path

from ..agents_crud import creatable_agents
from ..cluster import state as cluster_state
from ..file_access import (
    expand_user_path, is_dir, path_exists, read_text, tab_complete_paths,
)
from ..paths import DEFAULT_WORKSPACE, instance_state_dir_path
from ..tags import Registry
from ..tags.identity import SESSION_SEP, label_error, suggested_label
from .form_core import TextField

CONFIRM_PROMPT_FMT = "\n  {message}  (y/N) "
CONFIRM_YES_ANSWERS = ("y", "yes")
RETURN_TO_PICKER = "  Press Enter to return to the picker… "


def confirm_dialog(message: str) -> bool:
    """Inline yes/no prompt rendered below the (now-closed) picker."""
    answer = input(CONFIRM_PROMPT_FMT.format(message=message)).strip().lower()
    return answer in CONFIRM_YES_ANSWERS


def ask_for_find_term(current: str = "") -> str:
    """Prompt for alt+f's search term; "" when the user wants no find.

    Enter on an empty line CLEARS an active one (and does nothing when there
    is none), which is the only way back to the whole list without cancelling
    the picker. A term already in force is shown so the prompt says what it
    would replace. Asked on the plain terminal, like `confirm_dialog`: the
    picker has closed, and a modal inside it would have to re-implement a
    text field the forms already own."""
    shown = f" [{current}]" if current else ""
    prompt = f"Find in past conversations{shown} (Enter clears): "
    try:
        return input(prompt).strip()
    except EOFError:      # ^D at the prompt reads as "never mind"
        return ""


def _path_completer(text: str, state: int) -> str | None:
    """Tab-complete `text` as a host filesystem path; expands `~` for matching."""
    matches = tab_complete_paths(text)
    return matches[state] if state < len(matches) else None


def ask_for_workspace(agent: str, default: str | None = None) -> str:
    """Prompt for a workspace path; Enter uses `default` (or DEFAULT_WORKSPACE).
    Tab completes against the host filesystem. Returns the absolute path with `~`
    expanded but symlinks preserved — the form the user typed is what gets stored."""
    default = default if default is not None else DEFAULT_WORKSPACE
    prior_completer = readline.get_completer()
    prior_delims = readline.get_completer_delims()
    readline.set_completer(_path_completer)
    readline.set_completer_delims(" \t\n")
    if "libedit" in (readline.__doc__ or ""):
        readline.parse_and_bind("bind ^I rl_complete")  # macOS / BSD libedit syntax
    else:
        readline.parse_and_bind("tab: complete")        # GNU readline syntax
    try:
        while True:
            entered = input(
                f"Workspace path for '{agent}' instance [{default}]: "
            ).strip() or default
            absolute = expand_user_path(entered)
            if is_dir(absolute):
                return absolute
            print(f"Not a directory: {absolute}")
    finally:
        readline.set_completer(prior_completer)
        readline.set_completer_delims(prior_delims)


def _report_to_picker(message: str) -> None:
    """Show a one-off problem, then wait for Enter before the picker redraws.

    Seven call sites wrote this `print` + `input` pair out longhand (one of
    them with a stray leading newline in the pause, which is how the drift
    showed) — a stale cluster row, a member that vanished, a last member, an
    unresolvable tag set. The pause is the point: the picker's redraw would
    otherwise wipe the explanation off the screen before it could be read."""
    print(f"\n{message}")
    input(RETURN_TO_PICKER)


def _agent_description(md_text: str) -> str:
    """First line of an agent .md, stripped of any markdown heading marker — used as
    the right-hand description on a Create row in the picker. An empty .md
    yields "" rather than crashing the picker on splitlines()[0]."""
    return next(iter(md_text.splitlines()), "").lstrip("# ").strip()


def _agent_rows(registry: Registry) -> list[tuple[str, str]]:
    """Every pickable agent as (name, one-liner) — the membership form's list,
    in picker order (which the form's panel uses as its sort rank)."""
    return [(a.name, _agent_description(read_text(a.md_path)))
            for a in creatable_agents(registry)]


def _session_field_error(value: str, current: str | None = None) -> str | None:
    """The cluster-name field's live complaint, or None: the shared label rule
    (`tags.identity.label_error` — its message names the place the character
    would misbehave), then the collision rule. `current` is the name an EDIT
    arrived with — keeping your own name is never a collision (the same
    allowance the instance field gives)."""
    if (error := label_error(value)) is not None:
        return error
    if value != current and cluster_state.exists(value):
        return "a cluster of this name already exists"
    return None


def _project_field_error(value: str) -> str | None:
    """The project-path field's live complaint, or None. Same rule the
    workspace prompt enforces: it must BE a directory, `~` welcome."""
    if not value:
        return "cannot be empty"
    if not is_dir(expand_user_path(value)):
        return f"not a directory: {expand_user_path(value)}"
    return None


def _cluster_fields(project: str, session: str, *,
                    current: str | None = None,
                    derive: str | None = None) -> "list[TextField]":
    """The two text fields both cluster forms carry, live-validated. One
    builder so create and edit cannot drift in what they accept.

    PROJECT FIRST — it is what the name derives from: with `derive` (the
    template name, creation only), the name field auto-fills
    `<template>__<workspace-basename>` and keeps following the path as it is
    typed, until the user touches the name field — the same shape instance
    ids have (`<agent>__<session>`), and the same "basename is the default
    name" rule prompt_session used. The basename is bent legal first
    (`suggested_label`: `my.app` → `my-app`), so a dotted project dir never
    pre-fills a name the field then refuses. An edit passes no `derive`: the
    existing name sits still, renames are deliberate."""
    def auto_name(values: dict[str, str]) -> str:
        raw = values.get("project", "").strip()
        base = suggested_label(Path(expand_user_path(raw)).name) if raw else ""
        return f"{derive}__{base}" if base else str(derive)
    return [
        TextField(key="project", label="project path", value=project,
                  validate=_project_field_error),
        TextField(key="session", label="cluster name", value=session,
                  validate=lambda v: _session_field_error(v, current),
                  auto=auto_name if derive is not None else None),
    ]


def _suffix_field_error(agent: str, value: str,
                        current: str | None = None) -> str | None:
    """The instance-name field's live complaint, or None. First the shared
    label rule (`tags.identity.label_error`): the name becomes a directory,
    part of a docker `--name` and, under `{muxer}`, a tmux address —
    cluster/solo.py documented a dot from a dotted project dir reaching tmux
    that way, a gap closed here, at the field, since 2026-09-09. Then the
    collision rule prompt_session enforced: `<agent>__<value>` must not name
    an existing instance, except the one an edit arrived as."""
    if (error := label_error(value)) is not None:
        return error
    candidate = f"{agent}{SESSION_SEP}{value}"
    if value != current and path_exists(instance_state_dir_path(candidate)):
        return f"instance '{candidate}' already exists"
    return None


def instance_fields(agent: str, *, workspace: str | None = None,
                    suffix: str | None = None,
                    current: str | None = None) -> "list[TextField]":
    """The instance form's two text fields — project path FIRST, then the
    session name that completes `<agent>__<name>`, auto-derived from the
    path's basename until the user types their own (exactly the cluster
    form's rule, exactly prompt_session's old default), bent legal by
    `suggested_label` so a dotted project dir never pre-fills a name the
    field then refuses. An edit passes the stored values plus `current`,
    which pins the name (renames stay deliberate) and exempts it from its
    own collision check."""
    def auto_suffix(values: dict[str, str]) -> str:
        raw = values.get("workspace", "").strip()
        return (suggested_label(Path(expand_user_path(raw)).name) if raw else "") or agent
    return [
        TextField(key="workspace", label="project path",
                  value=workspace if workspace is not None else DEFAULT_WORKSPACE,
                  validate=_project_field_error),
        TextField(key="session", label=f"name  ({agent}__…)",
                  value=suffix or "",
                  validate=lambda v: _suffix_field_error(agent, v, current),
                  auto=auto_suffix if current is None else None),
    ]
