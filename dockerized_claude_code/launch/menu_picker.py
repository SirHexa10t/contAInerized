"""Interactive agent UI: full-screen picker (prompt_toolkit) plus supporting
line-prompt helpers for workspace path and session suffix. Pulls picker-entry
builders and state lookups from agents_crud; has no agent-domain logic.

Public API:

  select_agent()
      Run the agent/session picker (main menu + nested deletion submenu) until the
      user picks something or cancels. Discovers agents/instances and handles
      deletions internally.
      -> ('new', agent_dict) | ('cont', instance_dict) | None on cancel/empty

  ask_for_workspace(agent, default=None)
      Line prompt for a workspace path; tab-completes against the host filesystem.
      -> absolute path string

  prompt_session(agent, workspace)
      Line prompt for a session suffix; rejects collisions with existing instances.
      -> session suffix string

  prompt_auto(default=False)
      Y/N prompt for the {auto} mode opt-in (unattended-execution + firewall
      explainer); used by run.py on new instances and by select_agent's modify flow.
      -> bool

  prompt_dood(default=False)
      Y/N prompt for the {DooD} mode opt-in (with security explainer); used by run.py
      on new [prog] instances and by select_agent's modify flow.
      -> bool

  prompt_modes(tags, current_modes=())
      Run all applicable mode prompts in ORDERED_MODES priority order (auto, then
      DooD if [prog]); pre-fills defaults from the existing modes list. Used by
      run.py (new instances) and select_agent's modify flow.
      -> list[str] of newly-selected modes

  pick_with_preview(title, entries, *, allow_delete=False, allow_modify=False)
      Generic full-screen picker primitive used by select_agent.
      -> ('select', value) | ('delete', value) | ('modify', value) | (None, None)

  confirm_dialog(message)
      Inline [y/N] prompt.
      -> bool

Generic-picker entry shape (pick_with_preview):
    {
        'display':    str | list[(style, text)] | FormattedText,
        'preview':    str,
        'value':      any,    # opaque; returned to the caller on selection
        'deletable':  bool,   # optional; defaults True. When False, Del is a no-op on this row.
        'modifiable': bool,   # optional; defaults True. When False, F2 is a no-op on this row.
    }
"""

# ============================================================
# UI strings
# ============================================================

HINT_BASE_TEXT       = "↑↓ navigate  •  type to filter  •  Enter select  •  Esc cancel"
HINT_DELETE_SUFFIX   = "  •  Del delete"
HINT_MODIFY_SUFFIX   = "  •  F2 modify"
FILTER_LABEL         = "filter: "
EMPTY_FILTER_MESSAGE = "(no matches)"
DIVIDER_CHAR         = "│"
CONFIRM_PROMPT_FMT   = "{message}  [y/N]: "
CONFIRM_YES_ANSWERS  = ("y", "yes")

# ============================================================
# Layout
# ============================================================

LIST_WEIGHT    = 2
PREVIEW_WEIGHT = 3
TITLE_HEIGHT   = 1
STATUS_HEIGHT  = 2
DIVIDER_WIDTH  = 1
PAGE_JUMP      = 10  # rows skipped per PageUp/PageDown

# ============================================================
# Style class names + colors
# ============================================================

CLS_TITLE    = "picker-title"
CLS_DIVIDER  = "picker-divider"
CLS_STATUS   = "picker-status"
CLS_FILTER   = "picker-filter"
CLS_CURSOR   = "picker-cursor"
CLS_PREVIEW  = "picker-preview"
CLS_NO_MATCH = "picker-no-match"

STYLE_DICT = {
    CLS_TITLE:    "bold fg:ansibrightcyan",
    CLS_DIVIDER:  "fg:ansibrightblack",
    CLS_STATUS:   "fg:ansibrightblack",
    CLS_FILTER:   "bold fg:ansiyellow",
    CLS_CURSOR:   "reverse",
    CLS_PREVIEW:  "",
    CLS_NO_MATCH: "italic fg:ansibrightblack",
}

# ============================================================
# Agent-picker UI strings
# ============================================================

TITLE_AGENT_PICKER = "Select an agent:"
TITLE_DELETE_MENU  = "‼️  DELETE AGENT INSTANCES  ‼️"

MARKER_NEW    = "✨ Create"
MARKER_CONT   = "🏷️ Cont."
MARKER_DELMNU = "⚠️ DELETE‼️"
MARKER_DLET   = "🗑 DELETE"
MARKER_BACK   = "🚪  Back"

DELMENU_LABEL  = "(Move onto deletions menu)"
BACK_LABEL     = "(Move back to Agent Selection)"
DELMENU_PREVIEW = "Open the deletion sub-menu to remove agent instances and their state directories."
BACK_PREVIEW    = "Return to the main agent picker."
CONFIRM_DELETE_FMT = "Delete '{name}'?"

# ============================================================
# Agent-picker styles (inline, applied per-segment)
# ============================================================

STYLE_NEW_MARKER     = "fg:ansigreen"
STYLE_CONT_MARKER    = "fg:ansiyellow"
STYLE_DEL_MARKER     = "fg:ansired"
STYLE_AGENT_NAME     = "bold fg:ansibrightblue"
STYLE_DEL_NAME       = "bold fg:ansired"
STYLE_WORKSPACE_HINT = "italic fg:ansibrightblack"
STYLE_CURRENT_DIR    = "bold fg:ansiyellow"
STYLE_DEFAULT_DIR    = "bold fg:ansiyellow"   # same yellow as CURRENT DIR — kept as its own constant so colours can diverge later
STYLE_TAG            = "fg:ansibrightgreen"
STYLE_MODE_WARNING   = "bold fg:ansibrightred"   # DooD and other "elevated" modes — visual warning that the instance has reduced isolation

# ============================================================

import glob
import io
import os
import readline
from pathlib import Path

from prompt_toolkit import Application                                     # pip install prompt_toolkit
from prompt_toolkit.data_structures import Point
from prompt_toolkit.formatted_text import ANSI
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.keys import Keys
from prompt_toolkit.layout import HSplit, Layout, VSplit, Window
from prompt_toolkit.layout.controls import FormattedTextControl
from prompt_toolkit.layout.dimension import D
from prompt_toolkit.styles import Style
from rich.console import Console                                           # pip install rich
from rich.markdown import Markdown

from .agent_composition import MODE_AUTO, MODE_DOOD
from .agents_crud import (
    AGENTS_STATE, DEFAULT_WORKSPACE,
    creatable_agents, continuable_instances, delete_instance, instance_name, modify_instance,
    state_dir,
)


def _render_md(text):
    """Render markdown text to an ANSI-encoded string for the picker's preview pane.
    Width is fixed to 80; prompt_toolkit re-wraps if the pane is narrower."""
    buf = io.StringIO()
    Console(file=buf, force_terminal=True, color_system="truecolor", width=80).print(Markdown(text))
    return buf.getvalue()


def _agent_description(md_text):
    """First line of an agent .md, stripped of any markdown heading marker — used as
    the right-hand description on a Create row in the picker."""
    return md_text.splitlines()[0].lstrip("# ").strip()


def _create_preview(agent):
    """Build the Create-row preview markdown from a creatable_agents dict and render to ANSI.
    Italic source line, horizontal rule, then the .md content as-is."""
    return _render_md(
        f"*Create a new instance of `{agent['agent_name']}` — `agents/{agent['md_path'].name}`*\n\n"
        f"---\n\n"
        f"{agent['md_text']}"
    )


def _cont_preview(inst):
    """Build the Cont-row preview markdown from a continuable_instances dict and render to ANSI.
    Italic lead-in, horizontal rule, then a YAML-fenced metadata block (rich syntax-colors keys/values)."""
    return _render_md(
        f"*Continue session `{inst['id']}`.*\n\n"
        f"---\n\n"
        f"```yaml\n"
        f"Agent:     {inst['agent_name']}\n"
        f"Session:   {inst['session']}\n"
        f"Workspace: {inst['workspace_display']}\n"
        f"Modes:     {inst['modes_display']}\n"
        f"State:     {inst['state_path']}\n"
        f"Last used: {inst['last_used_display']}\n"
        f"```\n"
    )


def _normalize(display):
    """Coerce any accepted display form into a list of (style, text) tuples."""
    if isinstance(display, str):
        return [("", display)]
    return list(display)


def _tag_prefix_str(tags):
    """'[t1] [t2] ' for a non-empty tag list, '' for empty. Used to size the tag column
    consistently across Create rows (so agent names line up regardless of tags)."""
    return "".join(f"[{t}] " for t in tags) if tags else ""


def _mode_prefix_str(modes):
    """'{m1} {m2} ' for a non-empty mode list, '' for empty. Curly braces (vs.
    square brackets used by tags) distinguish modes — tags come from the agent's
    filename grammar, modes are per-instance opt-ins like DooD. Sizes the mode
    column on Cont rows so instance IDs align whether or not the instance has
    elevated modes."""
    return "".join(f"{{{m}}} " for m in modes) if modes else ""


def _plain(display):
    """Plain-text view of a display, used for filter matching."""
    return "".join(text for _, text in _normalize(display))


def pick_with_preview(title, entries, *, allow_delete=False, allow_modify=False):
    """Render a full-screen picker; block until the user picks or cancels."""
    if not entries:
        raise ValueError("entries must be non-empty")

    state = {
        "cursor": 0,
        "filter": "",
        "shown": list(range(len(entries))),
        "result": (None, None),
    }

    def refilter():
        q = state["filter"].lower()
        state["shown"] = [i for i in range(len(entries))
                          if q in _plain(entries[i]["display"]).lower()]
        if state["shown"] and state["cursor"] not in state["shown"]:
            state["cursor"] = state["shown"][0]
        elif not state["shown"]:
            state["cursor"] = 0

    def list_fragments():
        if not state["shown"]:
            return [(f"class:{CLS_NO_MATCH}", EMPTY_FILTER_MESSAGE)]
        out = []
        for i in state["shown"]:
            segments = _normalize(entries[i]["display"])
            if i == state["cursor"]:
                segments = [(f"class:{CLS_CURSOR} {style}".strip(), text)
                            for style, text in segments]
            out.extend(segments)
            out.append(("", "\n"))
        if out and out[-1] == ("", "\n"):
            out.pop()
        return out

    def preview_text():
        if not state["shown"]:
            return ""
        # Wrap in ANSI(...) so rich-rendered escape codes in Create-row previews show
        # as styled text. Plain previews (Cont rows, etc.) pass through unchanged.
        return ANSI(entries[state["cursor"]]["preview"])

    def title_fragments():
        return [(f"class:{CLS_TITLE}", title)]

    def status_fragments():
        hint = HINT_BASE_TEXT
        if allow_delete:
            hint += HINT_DELETE_SUFFIX
        if allow_modify:
            hint += HINT_MODIFY_SUFFIX
        out = [(f"class:{CLS_STATUS}", hint), ("", "\n")]
        if state["filter"]:
            out.append((f"class:{CLS_FILTER}", FILTER_LABEL))
            out.append(("", state["filter"]))
        return out

    def cursor_pos():
        if not state["shown"]:
            return Point(0, 0)
        return Point(0, state["shown"].index(state["cursor"]))

    kb = KeyBindings()

    def move(delta):
        if not state["shown"]:
            return
        n = len(state["shown"])
        i = state["shown"].index(state["cursor"])
        state["cursor"] = state["shown"][(i + delta) % n]

    @kb.add("up")
    def _(event): move(-1)

    @kb.add("down")
    def _(event): move(1)

    @kb.add("pageup")
    def _(event): move(-PAGE_JUMP)

    @kb.add("pagedown")
    def _(event): move(PAGE_JUMP)

    @kb.add("home")
    def _(event):
        if state["shown"]:
            state["cursor"] = state["shown"][0]

    @kb.add("end")
    def _(event):
        if state["shown"]:
            state["cursor"] = state["shown"][-1]

    @kb.add("enter")
    def _(event):
        if state["shown"]:
            state["result"] = ("select", entries[state["cursor"]]["value"])
            event.app.exit()

    @kb.add("escape")
    @kb.add("c-c")
    def _(event):
        state["result"] = (None, None)
        event.app.exit()

    @kb.add("backspace")
    def _(event):
        if state["filter"]:
            state["filter"] = state["filter"][:-1]
            refilter()

    @kb.add(Keys.Any)
    def _(event):
        ch = event.data
        if ch and len(ch) == 1 and ch.isprintable():
            state["filter"] += ch
            refilter()

    if allow_delete:
        @kb.add("delete")
        def _(event):
            if not state["shown"]:
                return
            entry = entries[state["cursor"]]
            if not entry.get("deletable", True):
                return  # silently ignored — caller marked this row non-deletable
            state["result"] = ("delete", entry["value"])
            event.app.exit()

    if allow_modify:
        @kb.add("f2")
        def _(event):
            if not state["shown"]:
                return
            entry = entries[state["cursor"]]
            if not entry.get("modifiable", True):
                return  # silently ignored — caller marked this row non-modifiable
            state["result"] = ("modify", entry["value"])
            event.app.exit()

    def accent_style():
        """Colour the preview's left-edge accent bar based on the selected row's kind:
        green for Create rows, yellow for Cont rows, dim default for menu/back rows."""
        if not state["shown"]:
            return f"class:{CLS_DIVIDER}"
        value = entries[state["cursor"]].get("value")
        kind = value[0] if isinstance(value, tuple) and value else None
        if kind == "new":
            return STYLE_NEW_MARKER     # fg:ansigreen
        if kind == "cont":
            return STYLE_CONT_MARKER    # fg:ansiyellow
        return f"class:{CLS_DIVIDER}"

    body = HSplit([
        Window(FormattedTextControl(title_fragments), height=TITLE_HEIGHT),
        VSplit([
            Window(
                FormattedTextControl(list_fragments,
                                     get_cursor_position=cursor_pos,
                                     focusable=True,
                                     show_cursor=False),
                wrap_lines=False,
                width=D(weight=LIST_WEIGHT),
            ),
            Window(width=DIVIDER_WIDTH, char=DIVIDER_CHAR, style=f"class:{CLS_DIVIDER}"),
            Window(width=1, char="▌", style=accent_style),   # preview-side accent bar; colour reflects selected row's kind
            Window(
                FormattedTextControl(preview_text),
                wrap_lines=True,
                width=D(weight=PREVIEW_WEIGHT),
                style=f"class:{CLS_PREVIEW}",
            ),
        ]),
        Window(FormattedTextControl(status_fragments), height=STATUS_HEIGHT),
    ])

    Application(
        layout=Layout(body),
        key_bindings=kb,
        style=Style.from_dict(STYLE_DICT),
        full_screen=True,
    ).run()

    return state["result"]


def confirm_dialog(message):
    """Inline yes/no prompt rendered below the (now-closed) picker."""
    answer = input(CONFIRM_PROMPT_FMT.format(message=message)).strip().lower()
    return answer in CONFIRM_YES_ANSWERS


def _path_completer(text, state):
    """Tab-complete `text` as a host filesystem path; expands `~` for matching."""
    matches = glob.glob(os.path.expanduser(text) + "*")
    matches = [m + os.sep if os.path.isdir(m) else m for m in matches]
    return matches[state] if state < len(matches) else None


def ask_for_workspace(agent, default=None):
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
            absolute = os.path.abspath(os.path.expanduser(entered))
            if Path(absolute).is_dir():
                return absolute
            print(f"Not a directory: {absolute}")
    finally:
        readline.set_completer(prior_completer)
        readline.set_completer_delims(prior_delims)


def prompt_session(agent, workspace):
    """Prompt for a session suffix; default = last segment of the workspace path.
    Rejects collisions with existing `{agent}__{suffix}` state dirs."""
    default = Path(workspace).name
    while True:
        suffix = input(f"Session suffix for '{agent}' [{default}]: ").strip() or default
        if not suffix:
            print("Session suffix cannot be empty.")
            continue
        if state_dir(agent, suffix).exists():
            print(f"Instance '{instance_name(agent, suffix)}' already exists. Pick another name.")
            continue
        return suffix


def prompt_yn(header, body, prompt_label, default=False):
    """Generic multi-line Y/N prompt. `header` is the question line, `body` is a
    list of explanation/caveat lines (empty strings render as blank lines for
    visual separation), and `prompt_label` is what shows in the actual y/N input
    (e.g. '{auto}'). Returns bool; Enter alone uses `default`."""
    print()
    print(f"  {header}")
    for line in body:
        print(f"  {line}" if line else "")
    default_marker = "Y/n" if default else "y/N"
    answer = input(f"  Enable {prompt_label}? [{default_marker}]: ").strip().lower()
    if not answer:
        return default
    return answer in ("y", "yes")


def prompt_auto(default=False):
    """Y/N prompt for opting into {auto} mode."""
    return prompt_yn(
        header="Auto / unattended mode?",
        body=[
            "Lets the agent run continuously without per-action permission prompts",
            "(passes --dangerously-skip-permissions to claude). The container runs",
            "behind an iptables outbound allowlist, so the agent can only reach",
            "Anthropic, GitHub, npm, PyPI, crates.io and DNS — anything else is",
            "dropped at the network layer.",
            "",
            "⚠ Even with the firewall, the agent has full filesystem write access",
            "  in its workspace and can run arbitrary code there. Use only for",
            "  tasks where you trust the agent to act on its own.",
        ],
        prompt_label="{auto}",
        default=default,
    )


def prompt_dood(default=False):
    """Y/N prompt for opting into {DooD} (Docker-out-of-Docker) mode."""
    return prompt_yn(
        header="Docker-out-of-Docker (DooD) mode?",
        body=[
            "This is for agents that need to run their own Docker containers",
            "(e.g., to test a project that uses docker compose). Without it,",
            "the agent can't reach the host's Docker daemon.",
            "",
            "⚠ Avoid unless you actually need it. DooD bind-mounts",
            "  /var/run/docker.sock, which gives the container effective root",
            "  on the host (it can start any container as root, read host",
            "  paths via volume mounts, etc.).",
        ],
        prompt_label="{DooD}",
        default=default,
    )


def prompt_modes(tags, current_modes=()):
    """Prompt for each mode in MODE_HANDLERS priority order, applying per-mode
    applicability gates (DooD only fires for [prog] agents). `current_modes` is
    the existing list (pre-fills the Y/N defaults — empty for new instances).
    Returns the new modes in priority order — used by run.py for new instances
    and by select_agent's modify flow."""
    new_modes = []
    if prompt_auto(default=(MODE_AUTO in current_modes)):
        new_modes.append(MODE_AUTO)
    if "prog" in tags and prompt_dood(default=(MODE_DOOD in current_modes)):
        new_modes.append(MODE_DOOD)
    return new_modes


def select_agent():
    """Run the agent picker (main + nested deletion submenu) until selection or cancel.
    Caller must ensure at least one agent .md exists before invoking."""
    while True:
        agents = creatable_agents()
        instances = continuable_instances()

        instances_by_agent = {}
        for inst in instances:
            instances_by_agent.setdefault(inst["agent_name"], []).append(inst)

        agent_name_width = max(len(a["agent_name"]) for a in agents)
        instance_name_width = max((len(i["id"]) for i in instances), default=0)

        # Shared tag/mode column: tags only appear on Create rows, modes only on Cont rows,
        # so they never collide and can occupy the same horizontal slot. Width is sized to
        # the widest of either so the agent-name / instance-ID column still lines up.
        # Effect: a `{DooD}` mark on a Cont row sits at the same column as `[prog]` would
        # on a Create row (just nested by the Cont marker's longer prefix).
        tag_strs = [_tag_prefix_str(a.get("tags", [])) for a in agents]
        mode_strs_by_inst = {i["id"]: _mode_prefix_str(i.get("modes", [])) for i in instances}
        shared_col_width = max(
            [len(s) for s in tag_strs] + [len(s) for s in mode_strs_by_inst.values()],
            default=0,
        )

        entries = []
        for agent, tag_str in zip(agents, tag_strs):
            entries.append({
                "display": [
                    (STYLE_NEW_MARKER, f"{MARKER_NEW}  "),
                    (STYLE_TAG, tag_str),
                    ("", " " * (shared_col_width - len(tag_str))),
                    (STYLE_AGENT_NAME, f"{agent['agent_name']:<{agent_name_width}}"),
                    ("", f" — {_agent_description(agent['md_text'])}"),
                ],
                "preview": _create_preview(agent),
                "value": ("new", agent),
                "deletable": False,
                "modifiable": False,
            })
            for inst in instances_by_agent.get(agent["agent_name"], []):
                mode_str = mode_strs_by_inst[inst["id"]]
                cont_display = [
                    (STYLE_CONT_MARKER, f"{MARKER_CONT}      "),
                    (STYLE_MODE_WARNING, mode_str),
                    ("", " " * (shared_col_width - len(mode_str))),
                    (STYLE_AGENT_NAME, f"{inst['id']:<{instance_name_width}}"),
                    ("", "    "),
                ]
                if inst.get("is_current_dir"):
                    cont_display.append((STYLE_CURRENT_DIR, "(CURRENT DIR) "))
                elif inst.get("is_default_dir"):
                    cont_display.append((STYLE_DEFAULT_DIR, "(DEFAULT DIR) "))
                cont_display.append((STYLE_WORKSPACE_HINT, inst["workspace_display"]))
                entries.append({
                    "display": cont_display,
                    "preview": _cont_preview(inst),
                    "value": ("cont", inst),
                })

        entries.append({
            "display": [
                (STYLE_DEL_MARKER, f"{MARKER_DELMNU}  "),
                ("", DELMENU_LABEL),
            ],
            "preview": DELMENU_PREVIEW,
            "value": ("delmenu",),
            "deletable": False,
            "modifiable": False,
        })

        action, value = pick_with_preview(TITLE_AGENT_PICKER, entries, allow_delete=True, allow_modify=True)
        if action is None:
            return None

        if action == "delete":  # picker enforces deletability — only ('cont', inst) values reach here
            inst = value[1]
            if confirm_dialog(CONFIRM_DELETE_FMT.format(name=inst["id"])):
                delete_instance(inst["id"])
            continue

        if action == "modify":  # picker enforces modifiability — only ('cont', inst) values reach here
            inst = value[1]
            agent_name = inst["agent_name"]
            while True:
                new_session = input(f"New session suffix for '{agent_name}' [{inst['session']}]: ").strip() or inst["session"]
                if new_session == inst["session"]:
                    break  # keeping the same session — no collision possible
                if not (AGENTS_STATE / instance_name(agent_name, new_session)).exists():
                    break  # not colliding with an existing instance
                print(f"Instance '{instance_name(agent_name, new_session)}' already exists. Pick another name.")
            new_workspace = ask_for_workspace(agent_name, default=inst["workspace"])
            new_modes = prompt_modes(inst.get("tags", []), inst.get("modes", []))
            modify_instance(inst["id"], agent_name, new_session, new_workspace, new_modes)
            continue

        if value[0] == "delmenu":
            _delete_submenu()
            continue

        return value  # ('new', agent_dict) | ('cont', instance_dict)


def _delete_submenu():
    """Flat deletion submenu — every row red. Loops until Esc / Back."""
    while True:
        instances = continuable_instances()
        if not instances:
            return
        entries = []
        for inst in instances:
            entries.append({
                "display": [
                    (STYLE_DEL_MARKER, f"{MARKER_DLET}  "),
                    (STYLE_DEL_NAME, inst["id"]),
                ],
                "preview": _cont_preview(inst),
                "value": inst["id"],
            })
        entries.append({
            "display": [("", f"{MARKER_BACK}  {BACK_LABEL}")],
            "preview": BACK_PREVIEW,
            "value": None,
            "deletable": False,
        })

        action, value = pick_with_preview(TITLE_DELETE_MENU, entries, allow_delete=True)
        if action is None or value is None:
            return
        if confirm_dialog(CONFIRM_DELETE_FMT.format(name=value)):
            delete_instance(value)
