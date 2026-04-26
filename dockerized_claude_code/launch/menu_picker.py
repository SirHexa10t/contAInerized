"""Agent picker UI built on prompt_toolkit. Pulls picker-entry builders from agents_lib
(creatable_agents, continuable_instances, delete_instance); has no agent-domain logic.

Public API:

  select_agent()
      Run the agent/session picker (main menu + nested deletion submenu) until the
      user picks something or cancels. Discovers agents/instances and handles
      deletions internally.
      -> ('new', agent_dict) | ('cont', instance_dict) | None on cancel/empty

  pick_with_preview(title, entries, *, allow_delete=False)
      Generic full-screen picker primitive used by select_agent.
      -> ('select', value) | ('delete', value) | (None, None)

  confirm_dialog(message)
      Inline [y/N] prompt.
      -> bool

Generic-picker entry shape (pick_with_preview):
    {
        'display':   str | list[(style, text)] | FormattedText,
        'preview':   str,
        'value':     any,   # opaque; returned to the caller on selection
        'deletable': bool,  # optional; defaults True. When False, Del is a no-op on this row.
    }
"""

# ============================================================
# UI strings
# ============================================================

HINT_BASE_TEXT       = "↑↓ navigate  •  type to filter  •  Enter select  •  Esc cancel"
HINT_DELETE_SUFFIX   = "  •  Del delete"
HINT_REDEFINE_SUFFIX = "  •  F2 redefine"
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
MARKER_CONT   = " 🏷️ Cont."
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

# ============================================================

import os
from pathlib import Path

from prompt_toolkit import Application                                     # pip install prompt_toolkit
from prompt_toolkit.data_structures import Point
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.keys import Keys
from prompt_toolkit.layout import HSplit, Layout, VSplit, Window
from prompt_toolkit.layout.controls import FormattedTextControl
from prompt_toolkit.layout.dimension import D
from prompt_toolkit.styles import Style

from .agents_lib import (
    AGENTS_STATE, DEFAULT_WORKSPACE,
    creatable_agents, continuable_instances, delete_instance, instance_name, redefine_instance,
)


def _normalize(display):
    """Coerce any accepted display form into a list of (style, text) tuples."""
    if isinstance(display, str):
        return [("", display)]
    return list(display)


def _plain(display):
    """Plain-text view of a display, used for filter matching."""
    return "".join(text for _, text in _normalize(display))


def pick_with_preview(title, entries, *, allow_delete=False, allow_redefine=False):
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
        return entries[state["cursor"]]["preview"]

    def title_fragments():
        return [(f"class:{CLS_TITLE}", title)]

    def status_fragments():
        hint = HINT_BASE_TEXT
        if allow_delete:
            hint += HINT_DELETE_SUFFIX
        if allow_redefine:
            hint += HINT_REDEFINE_SUFFIX
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

    if allow_redefine:
        @kb.add("f2")
        def _(event):
            if not state["shown"]:
                return
            entry = entries[state["cursor"]]
            if not entry.get("redefinable", True):
                return  # silently ignored — caller marked this row non-redefinable
            state["result"] = ("redefine", entry["value"])
            event.app.exit()

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


def ask_for_workspace(agent, default=None):
    """Prompt for a workspace path; Enter uses `default` (or DEFAULT_WORKSPACE).
    Returns the absolute path, with `~` expanded but symlinks preserved — the form
    the user typed is what gets stored."""
    default = default if default is not None else DEFAULT_WORKSPACE
    while True:
        entered = input(
            f"Workspace path for '{agent}' instance [{default}]: "
        ).strip() or default
        absolute = os.path.abspath(os.path.expanduser(entered))
        if Path(absolute).is_dir():
            return absolute
        print(f"Not a directory: {absolute}")


def select_agent():
    """Run the agent picker (main + nested deletion submenu) until selection or cancel.
    Caller must ensure at least one agent .md exists before invoking."""
    while True:
        agents = creatable_agents()
        instances = continuable_instances()

        instances_by_agent = {}
        for inst in instances:
            instances_by_agent.setdefault(inst["agent_name"], []).append(inst)

        agent_name_width = max(len(a["label_name"]) for a in agents)
        instance_name_width = max((len(i["id"]) for i in instances), default=0)

        entries = []
        for agent in agents:
            entries.append({
                "display": [
                    (STYLE_NEW_MARKER, f"{MARKER_NEW}  "),
                    (STYLE_AGENT_NAME, f"{agent['label_name']:<{agent_name_width}}"),
                    ("", f" — {agent['description']}"),
                ],
                "preview": agent["preview"],
                "value": ("new", agent),
                "deletable": False,
                "redefinable": False,
            })
            for inst in instances_by_agent.get(agent["agent_name"], []):
                cont_display = [
                    (STYLE_CONT_MARKER, f"{MARKER_CONT}      "),
                    (STYLE_AGENT_NAME, f"{inst['id']:<{instance_name_width}}"),
                    ("", "    "),
                ]
                if inst.get("is_current_dir"):
                    cont_display.append((STYLE_CURRENT_DIR, "(CURRENT DIR) "))
                cont_display.append((STYLE_WORKSPACE_HINT, inst["workspace_display"]))
                entries.append({
                    "display": cont_display,
                    "preview": inst["preview"],
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
            "redefinable": False,
        })

        action, value = pick_with_preview(TITLE_AGENT_PICKER, entries, allow_delete=True, allow_redefine=True)
        if action is None:
            return None

        if action == "delete":  # picker enforces deletability — only ('cont', inst) values reach here
            inst = value[1]
            if confirm_dialog(CONFIRM_DELETE_FMT.format(name=inst["id"])):
                delete_instance(inst["id"])
            continue

        if action == "redefine":  # picker enforces redefinability — only ('cont', inst) values reach here
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
            redefine_instance(inst["id"], agent_name, new_session, new_workspace)
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
                "preview": inst["preview"],
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
