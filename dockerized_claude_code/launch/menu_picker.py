"""Interactive agent UI: full-screen picker (prompt_toolkit) plus supporting
line-prompt helpers for workspace path and session suffix. Pulls picker-entry
builders and state lookups from agents_crud; has no agent-domain logic.

Public API:

  select_agent()
      Run the agent/session picker (main menu + nested deletion submenu) until the
      user picks something or cancels. Discovers agents/instances and handles
      deletions internally.
      -> ('new', AgentIdentity) | ('cont', SessionIdentity) | None on cancel/empty

  ask_for_workspace(agent, default=None)
      Line prompt for a workspace path; tab-completes against the host filesystem.
      -> absolute path string

  prompt_session(agent, workspace)
      Line prompt for a session suffix; rejects collisions with existing instances.
      -> session suffix string

  prompt_auto(current_modifiers)
      Y/N prompt for the {auto} mode opt-in (unattended-execution + firewall
      explainer); used by run.py on new instances and by select_agent's modify flow.
      `current_modifiers` is the union of tags + active modes — used to pre-fill
      the Y/N default. Thin wrapper over prompt_modifier.
      -> bool

  prompt_dood(current_modifiers)
      Y/N prompt for the {DooD} mode opt-in (with security explainer); used by run.py
      on new [prog] instances and by select_agent's modify flow. Thin wrapper over
      prompt_modifier.
      -> bool

  prompt_modes(tags, current_modes=())
      Run all applicable mode prompts in InstanceModifiers.modes() priority order (auto, then
      DooD if [prog]); pre-fills defaults from the existing modes list. Used by
      run.py (new instances) and select_agent's modify flow.
      -> list[str] of newly-selected modes

  pick_with_preview(title, entries, *, allow_delete=False, allow_modify=False)
      Generic full-screen picker primitive used by select_agent.
      -> (PickerAction.SELECT, value) | (PickerAction.DELETE, value)
         | (PickerAction.MODIFY, value) | (None, None) on cancel

  confirm_dialog(message)
      Inline [y/N] prompt.
      -> bool

  print_launch_banner(sess_id, cred_names)
      Print the multi-line "about to launch" summary (agent definition, conf,
      tags, modes, skills, creds, user whitelist count) before docker compose
      builds the image. Conditional lines for tags/modes/skills/creds/whitelist
      — only shown when applicable. md_path / conf_path / tags / modes all come
      off the SessionIdentity. The user-whitelist line counts
      user_firewall_whitelist_lines() on demand only when {auto} is in modes.

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
HINT_LEGEND_SUFFIX   = "  •  F8 legend"
HINT_LEGEND_OPEN     = "F8 / Esc close legend"
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

# Style class names + their corresponding style strings live as the
# PickerClass enum below — defined after the imports because dataclass /
# enum decorators need their stdlib modules in scope first.

# ============================================================
# Agent-picker UI strings
# ============================================================

TITLE_AGENT_PICKER = "Select an agent:"
TITLE_DELETE_MENU  = "‼️  DELETE AGENT INSTANCES  ‼️"

# Row marker glyphs + their styles live on the PickerRowMarker enum (after the
# imports). Cwd-relation labels ("(CURRENT DIR) " / "(DEFAULT DIR) ") live on
# PickerCwdHint there too.

DELMENU_LABEL  = "(Move onto deletions menu)"
BACK_LABEL     = "(Move back to Agent Selection)"
DELMENU_PREVIEW = "Open the deletion sub-menu to remove agent instances and their state directories."
BACK_PREVIEW    = "Return to the main agent picker."
CONFIRM_DELETE_FMT = "Delete '{name}'?"

# ============================================================
# Agent-picker styles (inline, applied per-segment)
# ============================================================

STYLE_AGENT_NAME     = "bold fg:ansibrightblue"
STYLE_DEL_NAME       = "bold fg:ansired"
STYLE_WORKSPACE_HINT = "italic fg:ansibrightblack"
STYLE_TAG            = "fg:ansibrightgreen"
STYLE_MODE_WARNING   = "bold fg:ansibrightred"   # DooD and other "elevated" modes — visual warning that the instance has reduced isolation

# ============================================================

import dataclasses
import io
import readline
from enum import Enum
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
from rich.theme import Theme

from .agents_crud import (
    continuable_instances, creatable_agents, delete_instance, modify_instance,
)
from .file_access import (
    expand_user_path, home_dir, is_dir, path_exists, tab_complete_paths,
    user_firewall_whitelist_lines,
)
from .paths import DEFAULT_WORKSPACE, DOCKERIZED_CLAUDE_ROOT, FIREWALL_WHITELIST_FILE
from .structs import AgentIdentity, InstanceIdentity, InstanceModifiers
from .utils import plural


class PickerAction(Enum):
    """Closed set of actions pick_with_preview returns alongside the selected
    entry's value. None (returned for cancel/escape) sits outside the enum so
    callers can branch on `if action is None` idiomatically."""
    SELECT = "select"     # Enter — user picked a row
    DELETE = "delete"     # Del   — user pressed delete on a row (only fires for deletable rows)
    MODIFY = "modify"     # F2    — user pressed modify on a row (only fires for modifiable rows)


class PickerClass(Enum):
    """prompt_toolkit CSS-like class names + the style applied to spans tagged
    with each. Bundling both on the enum member keeps the class name and its
    style co-located (adding a new entry threads through STYLE_DICT below
    without a second hand-maintained list). Members expose:
      .cls_name — the CSS-like class string used in prompt_toolkit style refs
      .style    — the prompt_toolkit style string applied to that class
      .css      — `class:<cls_name>` — what span tuples want as the style key
    """
    TITLE    = ("picker-title",    "bold fg:ansibrightcyan")
    DIVIDER  = ("picker-divider",  "fg:ansibrightblack")
    STATUS   = ("picker-status",   "fg:ansibrightblack")
    FILTER   = ("picker-filter",   "bold fg:ansiyellow")
    CURSOR   = ("picker-cursor",   "reverse")
    PREVIEW  = ("picker-preview",  "")
    NO_MATCH = ("picker-no-match", "italic fg:ansibrightblack")

    def __init__(self, cls_name: str, style: str) -> None:
        self.cls_name = cls_name
        self.style = style

    @property
    def css(self) -> str:
        return f"class:{self.cls_name}"


STYLE_DICT = {e.cls_name: e.style for e in PickerClass}


class PickerRowMarker(Enum):
    """Row prefix marker — pairs the glyph that prefixes a row with the style
    applied to it. Bundling so that 'kind of row' is one named thing instead of
    a (glyph, style) pair manually assembled at each call site. The shared
    DEL_MARKER style is preserved by giving DELMNU and DLET the same colour
    string — they're two different *markers* that happen to render the same.

    Members expose:
      .glyph      — the marker text (emoji + label)
      .style      — prompt_toolkit style applied to the glyph
      .fragment() — (style, glyph+suffix) tuple ready for a FormattedText segment
    """
    NEW    = ("✨ Create",       "fg:ansigreen")
    CONT   = ("🏷️ Cont.",        "fg:ansiyellow")
    DELMNU = ("⚠️ DELETE‼️",     "fg:ansired")
    DLET   = ("🗑 DELETE",       "fg:ansired")
    BACK   = ("🚪  Back",        "")

    def __init__(self, glyph: str, style: str) -> None:
        self.glyph = glyph
        self.style = style

    def fragment(self, suffix: str = "") -> tuple[str, str]:
        """Build the (style, text) tuple FormattedText expects — glyph then an
        arbitrary suffix (spacing for column alignment, or extra trailing text
        like the back-row's label) in this marker's style."""
        return (self.style, f"{self.glyph}{suffix}")


class PickerCwdHint(Enum):
    """The cwd-relation tag shown on a Cont row when the instance's workspace
    happens to coincide with where the launcher was invoked from. Same
    bundling rationale as PickerRowMarker — the label text and its style are
    a fixed pair, not two parallel constants. Both currently share a yellow
    style; kept as separate enum members so the colours can diverge later
    without re-threading call sites."""
    CURRENT = ("(CURRENT DIR) ", "bold fg:ansiyellow")
    DEFAULT = ("(DEFAULT DIR) ", "bold fg:ansiyellow")

    def __init__(self, label: str, style: str) -> None:
        self.label = label
        self.style = style

    @property
    def fragment(self) -> tuple[str, str]:
        """(style, label) tuple ready for a FormattedText segment. Property
        rather than method since the label is fixed — no per-call suffix."""
        return (self.style, self.label)


# Sentinel entry value signalling "open the delete submenu" — used in the
# main picker where most rows hold an identity dataclass; this is the one
# non-identity row, so a distinct singleton lets the dispatcher match by
# `is` rather than tagging identities with extra metadata.
_OPEN_DELMENU = object()


def _render_md(text: str, *, theme: dict | None = None) -> str:
    """Render markdown text to an ANSI-encoded string for the picker's preview pane.
    Width is fixed to 80; prompt_toolkit re-wraps if the pane is narrower. Optional
    `theme` (dict of Rich style names → style strings) overrides Markdown's defaults
    for this render — used by the legend to colour-code tag vs mode entries."""
    buf = io.StringIO()
    Console(
        file=buf, force_terminal=True, color_system="truecolor", width=80,
        theme=Theme(theme) if theme else None,
    ).print(Markdown(text))
    return buf.getvalue()


def _build_composition_legend() -> str:
    """Build the F8 'composition legend' shown over the preview pane. Rendered
    via _render_md so it matches Create-row previews stylistically. Tags and
    Modes are rendered as two separate markdown documents so each can override
    `markdown.code` (green for tag names, bold red for mode names) — matching
    the picker's per-row marker colours (STYLE_TAG / STYLE_MODE_WARNING)."""
    rows_tags = "\n".join(
        f"| `{m.value}` | {m.description} |"
        for m in InstanceModifiers.tags()
    )
    rows_modes = "\n".join(
        f"| `{m.value}` | {m.description} |"
        for m in InstanceModifiers.modes()
    )
    tags_md = (
        "# Tags\n\n"
        "Agent core-affinities; dictate requirements and tools.\n\n"
        "| Tag | Description |\n"
        "|-----|-------------|\n"
        f"{rows_tags}\n"
    )
    modes_md = (
        "# Modes\n\n"
        "Special approach/capability activation.\n\n"
        "| Mode | Description |\n"
        "|------|-------------|\n"
        f"{rows_modes}\n"
    )
    return (
        _render_md(tags_md,  theme={"markdown.code": "bright_green"})
        + _render_md(modes_md, theme={"markdown.code": "bold bright_red"})
    )


LEGEND_TEXT = _build_composition_legend()   # module-level so the picker doesn't rebuild on every keypress


def _agent_description(md_text: str) -> str:
    """First line of an agent .md, stripped of any markdown heading marker — used as
    the right-hand description on a Create row in the picker."""
    return md_text.splitlines()[0].lstrip("# ").strip()


def _create_preview(agent: dict) -> str:
    """Build the Create-row preview markdown from a creatable_agents entry and
    render to ANSI. Italic source line, horizontal rule, then the .md content as-is."""
    agent_id = agent["identity"]
    return _render_md(
        f"*Create a new instance of `{agent_id.agent}` — `agents/{agent['md_path'].name}`*\n\n"
        f"---\n\n"
        f"{agent['md_text']}"
    )


def _cont_preview(inst: dict) -> str:
    """Build the Cont-row preview markdown from a continuable_instances entry and
    render to ANSI. Italic lead-in, horizontal rule, then a YAML-fenced metadata
    block (rich syntax-colors keys/values)."""
    sess_id = inst["identity"]
    return _render_md(
        f"*Continue session `{sess_id.instance}`.*\n\n"
        f"---\n\n"
        f"```yaml\n"
        f"Agent:     {sess_id.agent}\n"
        f"Session:   {sess_id.session}\n"
        f"Workspace: {inst['workspace_display']}\n"
        f"Modes:     {inst['modes_display']}\n"
        f"State:     {sess_id.state_dir}\n"
        f"Last used: {inst['last_used_display']}\n"
        f"```\n"
    )


def _normalize(display) -> list[tuple[str, str]]:
    """Coerce any accepted display form into a list of (style, text) tuples."""
    if isinstance(display, str):
        return [("", display)]
    return list(display)


def _plain(display) -> str:
    """Plain-text view of a display, used for filter matching."""
    return "".join(text for _, text in _normalize(display))


def pick_with_preview(title: str, entries: list, *, allow_delete: bool = False, allow_modify: bool = False, legend_text: str | None = None):
    """Render a full-screen picker; block until the user picks or cancels.

    legend_text — optional ANSI string. When provided, F8 toggles it as an overlay
    over the preview pane (Esc closes it). The agent picker passes LEGEND_TEXT so
    users can recall what each [tag] / {mode} marker means."""
    if not entries:
        raise ValueError("entries must be non-empty")

    state = {
        "cursor": 0,
        "filter": "",
        "shown": list(range(len(entries))),
        "result": (None, None),
        "legend_open": False,
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
            return [(PickerClass.NO_MATCH.css, EMPTY_FILTER_MESSAGE)]
        out = []
        for i in state["shown"]:
            segments = _normalize(entries[i]["display"])
            if i == state["cursor"]:
                segments = [(f"{PickerClass.CURSOR.css} {style}".strip(), text)
                            for style, text in segments]
            out.extend(segments)
            out.append(("", "\n"))
        if out and out[-1] == ("", "\n"):
            out.pop()
        return out

    def preview_text():
        if state["legend_open"] and legend_text is not None:
            return ANSI(legend_text)
        if not state["shown"]:
            return ""
        # Wrap in ANSI(...) so rich-rendered escape codes in Create-row previews show
        # as styled text. Plain previews (Cont rows, etc.) pass through unchanged.
        return ANSI(entries[state["cursor"]]["preview"])

    def title_fragments():
        return [(PickerClass.TITLE.css, title)]

    def status_fragments():
        if state["legend_open"]:
            hint = HINT_LEGEND_OPEN
        else:
            hint = HINT_BASE_TEXT
            if allow_delete:
                hint += HINT_DELETE_SUFFIX
            if allow_modify:
                hint += HINT_MODIFY_SUFFIX
            if legend_text is not None:
                hint += HINT_LEGEND_SUFFIX
        out = [(PickerClass.STATUS.css, hint), ("", "\n")]
        if state["filter"]:
            out.append((PickerClass.FILTER.css, FILTER_LABEL))
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
            state["result"] = (PickerAction.SELECT, entries[state["cursor"]]["value"])
            event.app.exit()

    @kb.add("escape")
    def _(event):
        if state["legend_open"]:
            state["legend_open"] = False
            return
        state["result"] = (None, None)
        event.app.exit()

    @kb.add("c-c")
    def _(event):
        state["result"] = (None, None)
        event.app.exit()

    @kb.add("f8")
    def _(event):
        if legend_text is not None:
            state["legend_open"] = not state["legend_open"]

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
            state["result"] = (PickerAction.DELETE, entry["value"])
            event.app.exit()

    if allow_modify:
        @kb.add("f2")
        def _(event):
            if not state["shown"]:
                return
            entry = entries[state["cursor"]]
            if not entry.get("modifiable", True):
                return  # silently ignored — caller marked this row non-modifiable
            state["result"] = (PickerAction.MODIFY, entry["value"])
            event.app.exit()

    def accent_style():
        """Colour the preview's left-edge accent bar based on the selected row's kind:
        green for Create rows (AgentIdentity), yellow for Cont rows (InstanceIdentity
        and its SessionIdentity subclass), dim default for menu/back rows."""
        if not state["shown"]:
            return PickerClass.DIVIDER.css
        value = entries[state["cursor"]].get("value")
        if isinstance(value, InstanceIdentity):     # cont row — SessionIdentity is a subclass; checked before AgentIdentity since InstanceIdentity isa AgentIdentity
            return PickerRowMarker.CONT.style       # fg:ansiyellow
        if isinstance(value, AgentIdentity):        # new row — plain AgentIdentity only
            return PickerRowMarker.NEW.style        # fg:ansigreen
        return PickerClass.DIVIDER.css

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
            Window(width=DIVIDER_WIDTH, char=DIVIDER_CHAR, style=PickerClass.DIVIDER.css),
            Window(width=1, char="▌", style=accent_style),   # preview-side accent bar; colour reflects selected row's kind
            Window(
                FormattedTextControl(preview_text),
                wrap_lines=True,
                width=D(weight=PREVIEW_WEIGHT),
                style=PickerClass.PREVIEW.css,
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


def confirm_dialog(message: str) -> bool:
    """Inline yes/no prompt rendered below the (now-closed) picker."""
    answer = input(CONFIRM_PROMPT_FMT.format(message=message)).strip().lower()
    return answer in CONFIRM_YES_ANSWERS


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


def prompt_session(agent: str, workspace: str) -> str:
    """Prompt for a session suffix; default = last segment of the workspace path.
    Rejects collisions with existing `{agent}__{suffix}` state dirs."""
    default = Path(workspace).name
    while True:
        suffix = input(f"Session suffix for '{agent}' [{default}]: ").strip() or default
        if not suffix:
            print("Session suffix cannot be empty.")
            continue
        if path_exists(InstanceIdentity.state_dir_for(agent, suffix)):
            print(f"Instance '{InstanceIdentity.instance_name(agent, suffix)}' already exists. Pick another name.")
            continue
        return suffix


def prompt_yn(header: str, body, prompt_label: str, default: bool = False) -> bool:
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


def prompt_modifier(modifier, current_modifiers, *, header: str, body) -> bool:
    """Y/N prompt for opting into `modifier`. `current_modifiers` is an iterable
    of canonical-string modifier names (typically the union of tags + currently
    active modes) — used to pre-fill the Y/N default (True iff `modifier.value`
    is in there). Header / body explain the modifier's effect; the prompt label
    comes from the modifier's `.label` property. Shared body for prompt_auto /
    prompt_dood / future prompts: keep the per-modifier UX boilerplate (security
    explainer text) at the call site, keep the prompt mechanics here."""
    return prompt_yn(
        header=header,
        body=body,
        prompt_label=modifier.label,
        default=modifier.value in current_modifiers,
    )


def prompt_auto(current_modifiers) -> bool:
    """Y/N prompt for opting into {auto} mode."""
    return prompt_modifier(
        InstanceModifiers.MODE_AUTO,
        current_modifiers,
        header="Auto / unattended mode?",
        body=[
            "Lets the agent run continuously without per-action permission prompts",
            "(passes --dangerously-skip-permissions to claude). The container runs",
            "behind an iptables outbound whitelist, so the agent can only reach",
            "Anthropic, GitHub, npm, PyPI, crates.io and DNS — anything else is",
            "dropped at the network layer.",
            "",
            "⚠ Even with the firewall, the agent has full filesystem write access",
            "  in its workspace and can run arbitrary code there. Use only for",
            "  tasks where you trust the agent to act on its own.",
        ],
    )


def prompt_dood(current_modifiers) -> bool:
    """Y/N prompt for opting into {DooD} (Docker-out-of-Docker) mode."""
    return prompt_modifier(
        InstanceModifiers.MODE_DOOD,
        current_modifiers,
        header=f"Docker-out-of-Docker ({InstanceModifiers.MODE_DOOD.value}) mode?",
        body=[
            "This is for agents that need to run their own Docker containers",
            "(e.g., to test a project that uses docker compose). Without it,",
            "the agent can't reach the host's Docker daemon.",
            "",
            f"⚠ Avoid unless you actually need it. {InstanceModifiers.MODE_DOOD.value} bind-mounts",
            "  /var/run/docker.sock, which gives the container effective root",
            "  on the host (it can start any container as root, read host",
            "  paths via volume mounts, etc.).",
        ],
    )


def prompt_modes(tags, current_modes: tuple = ()) -> list[str]:
    """Prompt for each mode in InstanceModifiers.modes() priority order, applying
    per-mode applicability gates (DooD only fires for [prog] agents). `current_modes`
    is the existing list (pre-fills the Y/N defaults — empty for new instances).
    Returns the new modes in priority order — used by run.py for new instances
    and by select_agent's modify flow."""
    current_modifiers = [*tags, *current_modes]
    new_modes: list[str] = []
    if prompt_auto(current_modifiers):
        new_modes.append(InstanceModifiers.MODE_AUTO.value)   # type: ignore[arg-type]
    if InstanceModifiers.TAG_PROG.value in current_modifiers and prompt_dood(current_modifiers):
        new_modes.append(InstanceModifiers.MODE_DOOD.value)   # type: ignore[arg-type]
    return new_modes


def select_agent():   # returns AgentIdentity | SessionIdentity | None — too dynamic for a tight annotation
    """Run the agent picker (main + nested deletion submenu) until selection or cancel.
    Caller must ensure at least one agent .md exists before invoking."""
    while True:
        agents = creatable_agents()
        instances = continuable_instances()

        instances_by_agent = {}
        for inst in instances:
            instances_by_agent.setdefault(inst["identity"].agent, []).append(inst)

        agent_name_width = max(len(a["identity"].agent) for a in agents)
        instance_name_width = max((len(i["identity"].instance) for i in instances), default=0)

        # Shared tag/mode column: tags only appear on Create rows, modes only on Cont rows,
        # so they never collide and can occupy the same horizontal slot. Width is sized to
        # the widest of either so the agent-name / instance-ID column still lines up.
        # Effect: a `{DooD}` mark on a Cont row sits at the same column as `[prog]` would
        # on a Create row (just nested by the Cont marker's longer prefix).
        tag_strs = [InstanceModifiers.format_prefix(a.get("tags", [])) for a in agents]
        mode_strs_by_inst = {i["identity"].instance: InstanceModifiers.format_prefix(i["identity"].modes) for i in instances}
        shared_col_width = max(
            [len(s) for s in tag_strs] + [len(s) for s in mode_strs_by_inst.values()],
            default=0,
        )

        entries = []
        for agent, tag_str in zip(agents, tag_strs):
            agent_id = agent["identity"]
            entries.append({
                "display": [
                    PickerRowMarker.NEW.fragment("  "),
                    (STYLE_TAG, tag_str),
                    ("", " " * (shared_col_width - len(tag_str))),
                    (STYLE_AGENT_NAME, f"{agent_id.agent:<{agent_name_width}}"),
                    ("", f" — {_agent_description(agent['md_text'])}"),
                ],
                "preview": _create_preview(agent),
                "value": agent_id,
                "deletable": False,
                "modifiable": False,
            })
            for inst in instances_by_agent.get(agent_id.agent, []):
                sess_id = inst["identity"]
                mode_str = mode_strs_by_inst[sess_id.instance]
                cont_display = [
                    PickerRowMarker.CONT.fragment("      "),
                    (STYLE_MODE_WARNING, mode_str),
                    ("", " " * (shared_col_width - len(mode_str))),
                    (STYLE_AGENT_NAME, f"{sess_id.instance:<{instance_name_width}}"),
                    ("", "    "),
                ]
                if inst.get("is_current_dir"):
                    cont_display.append(PickerCwdHint.CURRENT.fragment)
                elif inst.get("is_default_dir"):
                    cont_display.append(PickerCwdHint.DEFAULT.fragment)
                cont_display.append((STYLE_WORKSPACE_HINT, inst["workspace_display"]))
                entries.append({
                    "display": cont_display,
                    "preview": _cont_preview(inst),
                    "value": sess_id,
                })

        entries.append({
            "display": [
                PickerRowMarker.DELMNU.fragment("  "),
                ("", DELMENU_LABEL),
            ],
            "preview": DELMENU_PREVIEW,
            "value": _OPEN_DELMENU,
            "deletable": False,
            "modifiable": False,
        })

        action, value = pick_with_preview(TITLE_AGENT_PICKER, entries, allow_delete=True, allow_modify=True, legend_text=LEGEND_TEXT)
        if action is None:
            return None

        if action == PickerAction.DELETE:  # picker enforces deletability — only cont rows (InstanceIdentity) reach here
            if confirm_dialog(CONFIRM_DELETE_FMT.format(name=value.instance)):
                delete_instance(value)
            continue

        if action == PickerAction.MODIFY:  # picker enforces modifiability — only cont rows reach here
            old_inst_id = value
            while True:
                new_session = input(f"New session suffix for '{old_inst_id.agent}' [{old_inst_id.session}]: ").strip() or old_inst_id.session
                if new_session == old_inst_id.session:
                    break  # keeping the same session — no collision possible
                if not path_exists(InstanceIdentity.state_dir_for(old_inst_id.agent, new_session)):
                    break  # not colliding with an existing instance
                print(f"Instance '{InstanceIdentity.instance_name(old_inst_id.agent, new_session)}' already exists. Pick another name.")
            new_workspace = ask_for_workspace(old_inst_id.agent, default=old_inst_id.workspace)
            new_modes = prompt_modes(old_inst_id.tags, old_inst_id.modes)
            new_sess_id = dataclasses.replace(
                old_inst_id, session=new_session, workspace=new_workspace, modes=tuple(new_modes)
            )  # is_brand_new stays False via the dataclass replace
            modify_instance(old_inst_id, new_sess_id)
            continue

        if value is _OPEN_DELMENU:
            _delete_submenu()
            continue

        return value  # AgentIdentity (new) | SessionIdentity (cont)


def _delete_submenu() -> None:
    """Flat deletion submenu — every row red. Loops until Esc / Back."""
    while True:
        instances = continuable_instances()
        if not instances:
            return
        entries = []
        for inst in instances:
            sess_id = inst["identity"]
            entries.append({
                "display": [
                    PickerRowMarker.DLET.fragment("  "),
                    (STYLE_DEL_NAME, sess_id.instance),
                ],
                "preview": _cont_preview(inst),
                "value": sess_id,
            })
        entries.append({
            "display": [PickerRowMarker.BACK.fragment(f"  {BACK_LABEL}")],
            "preview": BACK_PREVIEW,
            "value": None,
            "deletable": False,
        })

        action, value = pick_with_preview(TITLE_DELETE_MENU, entries, allow_delete=True, legend_text=LEGEND_TEXT)
        if action is None or value is None:
            return
        if confirm_dialog(CONFIRM_DELETE_FMT.format(name=value.instance)):
            delete_instance(value)


def print_launch_banner(sess_id, cred_names) -> None:
    """Print the multi-line summary that appears before docker compose builds the
    image — agent definition path, conf path, active tags + modes, and skills/creds
    counts when applicable. Each line is conditional on having something to show
    (no empty 'Tags: ' if there are none). The user-whitelist line counts
    user_firewall_whitelist_lines() inline — only when {auto} is in modes, so
    non-{auto} launches don't touch the file at all. Takes the launch's
    SessionIdentity and pulls md_path / conf_path / tags / modes off it directly."""
    print(f"  Agent definition: {sess_id.md_path.relative_to(DOCKERIZED_CLAUDE_ROOT)}")
    print(f"  Configuration:    {sess_id.conf_path.relative_to(DOCKERIZED_CLAUDE_ROOT) if sess_id.conf_path else '(none — using defaults)'}")
    if sess_id.tags:
        print(f"  Tags:             {' '.join(f'[{t}]' for t in sess_id.tags)}")
    if sess_id.modes:
        print(f"  Modes:            {' '.join('{' + m + '}' for m in sess_id.modes)}")
    if cred_names:
        print(f"  Optional creds:   {', '.join(cred_names)} (from user_extras/optional_creds/)")
    if InstanceModifiers.MODE_AUTO.value in sess_id.modes:
        whitelist_count = len(user_firewall_whitelist_lines())
        display_path = "~/" + str(FIREWALL_WHITELIST_FILE.relative_to(home_dir()))
        print(f"  User whitelist:   {whitelist_count} domain{plural(whitelist_count)} (from {display_path})")
